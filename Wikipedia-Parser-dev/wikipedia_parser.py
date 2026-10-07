"""
Wikipedia 单遍 XML 转储解析流水线

本模块提供一次性读取 Wikipedia XML（.xml.bz2）转储的入口，并将数据
并行写入两条输出路径：

  1. 原始表（latest） — 直接将每页的最新修订版记录写入 `wiki_latest_YYYYMMDD`表，结构固定，始终写入。

  2. 解析链 — 通过多进程池使用 mwparserfromhell 和 wikitextprocessor 对文章正文进行分段、提取组件（infobox、table、wikilinks 等）、生成段落和句子，最终写入 processed 表、8 类组件表以及层次表（sections / paragraph / wtp_intermediate / sentence）。

核心编排函数 `engine._run_core` 将批 XML 源、多进程解析和写入器解耦，提供背压、超时检测、首错捕获以及 checkpoint‑based 断点续传等特性。

设计要点
--------
* 单次读取——XML 文件仅被解压、流式读取一次；原始写入与解析并行进行，避免重复 I/O。
* 进程隔离——每个 worker 进程独立创建 WTP/Lua 状态，不会跨进程共享可变解析器状态。
* 失败可观测——解析异常不会中断全局导入，失败记录会被写入 processed 行的 `parse_error` 字段，并另行写入日志文件。
* 断点续传——可选的 SQL Server checkpoint 表记录每个修订版的终态，支持 `--resume` 与 `--retry-errors` 参数。

使用方法
--------
通过 ``wikipedia_parser.py`` 启动，配置文件 ``config.json`` 中填写 SQL Server 连接信息和转储路径。详见 ``README.md`` 中的参数说明。

文件结构
--------
* ``wikipedia_parser.py``——CLI、配置加载、writer 编排。
* ``pipeline/``——XML 来源、引擎核心、组件提取、WTP 整合、各类 writers。
* ``tests/``——单元与回归测试。
* ``docs/``——架构与开发指南。

注意：首次运行前请确保 ``config.json`` 包含有效的 SQL Server 主机、用户名、密码及数据库名，且转储文件路径正确。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from contextlib import redirect_stderr
from pathlib import Path
from typing import Callable

# pipeline/下的模块：XML 侧（原始写入器 + 流式批处理源）和处理引擎。将其放在 sys.path 第一位，这样多进程启动的 worker 也可以通过模块名重新导入 engine（其中包含 process_batch）。
sys.path.insert(0, str(Path(__file__).resolve().parent / "pipeline"))
import engine  # noqa: E402
from checkpoint import SQLServerCheckpointStore, source_id_for_path  # noqa: E402
from pipeline_config import (  # noqa: E402
    load_pipeline_config,
    output_table_names_for_dump,
    resolve_dump_file,
)
from run_logging import configure_error_logging, start_run_log  # noqa: E402
from wtp_database import ensure_wtp_database  # noqa: E402
from xml_source import (  # noqa: E402
    SQLServerWriter,
    SkippingBatchSource,
    XmlBatchSource,
)

"""为源类型返回一个可信的百分比分母。"""


# XML 的 max_pages 限制物理 <page> 元素。命名空间过滤和逻辑恢复过滤稍后进行，因此它不是 Read/Parse/Write/Raw 处理的计数。让这些流进度条只计数和速率。
def progress_total_for_source(*, is_xml: bool, max_pages: int | None) -> int | None:
    return None if is_xml else max_pages


# 构建命令行参数解析器
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="单遍 XML 转储读取：写入原始、处理、组件、WTP 和层次表。XML 输出表名从转储文件名获得 _YYYYMMDD 后缀。"
    )
    p.add_argument(
        "dump_file",
        type=Path,
        nargs="?",
        help="Wikipedia 转储路径 (*.xml.bz2)。省略时使用 source.dump_file。",
    )
    p.add_argument(
        "--config",
        type=Path,
        required=True,
        help="包含源、sqlserver 和 wtp 设置的统一配置 JSON 文件。",
    )
    p.add_argument(
        "--processed-table",
        default="wiki_processed",
        help="处理表名称（每篇文章一行；text_process = 用组件 id 替换组件的 wikitext）。默认 wiki_processed。",
    )
    p.add_argument(
        "--table-prefix",
        default="wiki_component",
        help="组件表基础名称的前缀；XML 输入写入 <prefix>_<type>_YYYYMMDD。默认 wiki_component。原始基础名称来自 config.table（默认 wiki_latest）。",
    )
    p.add_argument(
        "--sections-table",
        default="wiki_sections",
        help="Sections 表基础名称（每 section 一行；每 flush 在 revision_id 上删除然后插入）。默认 wiki_sections。",
    )
    p.add_argument(
        "--paragraphs-table",
        default="wiki_paragraph",
        help="Paragraphs 表基础名称（每 paragraph 一行；每 flush 在 revision_id 上删除然后插入）。默认 wiki_paragraph。",
    )
    p.add_argument(
        "--wtp-intermediate-table",
        default="wiki_wtp_intermediate",
        help="WTP 中间表（每 paragraph 一行：WTP 输入和扩展后的 Wikitext；每 revision_id 删除然后插入）。默认 wiki_wtp_intermediate。",
    )
    p.add_argument(
        "--sentences-table",
        default="wiki_sentence",
        help="Sentences 表基础名称（每 sentence 一行；每 flush 在 revision_id 上删除然后插入）。默认 wiki_sentence。",
    )
    p.add_argument(
        "--workers", type=int, default=None, help="解析进程数；默认使用 CPU 核心数。"
    )
    p.add_argument("--chunk-size", type=int, default=500, help="每批页数。")
    p.add_argument(
        "--write-batch", type=int, default=500, help="两个表的批量 upsert 大小。"
    )
    p.add_argument(
        "--max-pages", type=int, default=None, help="要处理的最大页数；省略则为全部。"
    )
    p.add_argument(
        "--namespace",
        type=int,
        action="append",
        default=None,
        help="只处理这些命名空间（可重复）。后续设置默认：0（文章）。使用 --all-namespaces 处理所有。",
    )
    p.add_argument(
        "--all-namespaces",
        action="store_true",
        help="处理所有命名空间（覆盖 --namespace）。",
    )
    p.add_argument(
        "--task-timeout",
        type=float,
        default=300.0,
        help="每批解析的最大等待时间（秒）；防止卡住/极慢的 worker 阻塞主流程；0 禁用。默认 300。",
    )
    p.add_argument(
        "--db-timeout",
        type=float,
        default=300.0,
        help="DB 查询超时（秒，两个表的读取/写入）；防止网络抖动/停滞服务器导致无限阻塞；0 禁用。默认 300。",
    )
    p.add_argument(
        "--db-login-timeout",
        type=float,
        default=60.0,
        help="DB 连接/登录超时（秒）。默认 60。",
    )
    p.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="对于 XML 输入，跳过 checkpoint 表中记录的终端修订版。默认：启用。",
    )
    p.add_argument(
        "--retry-errors",
        action="store_true",
        help="与 XML 恢复一起，重新处理 completed_with_errors 修订版。",
    )
    p.add_argument(
        "--checkpoint-table",
        default="wiki_parse_checkpoint",
        help="持久化 XML 恢复状态的表基础名称；XML 输入附加 _YYYYMMDD。默认 wiki_parse_checkpoint。",
    )
    return p


def _run(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    failure_logger: Callable[[str], None] | None = None,
    run_log_path: Path | None = None,
) -> None:
    import os

    # 1. 校验配置文件和转储文件
    if not args.config.exists():
        parser.error(f"--config 文件不存在：{args.config}")

    # 2. 加载统一配置，准备 SQL Server 和 WTP 设置
    unified_config = load_pipeline_config(args.config)
    config = unified_config.sqlserver
    wtp_settings = unified_config.wtp
    configured_dump_file = unified_config.dump_file
    dump_file = resolve_dump_file(
        cli_dump_file=args.dump_file,
        configured_dump_file=configured_dump_file,
    )

    # 参数边界验证
    if not dump_file.exists():
        parser.error(f"转储文件不存在：{dump_file}")
    try:
        wtp_db_path = ensure_wtp_database(dump_file, wtp_settings)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.error(f"无法准备 WTP 数据库：{exc}")
    wtp_settings = {**wtp_settings, "db_path": str(wtp_db_path)}
    if run_log_path is not None:
        wtp_settings["log_path"] = str(run_log_path)
    if args.workers is not None and args.workers < 1:
        parser.error("--workers 必须 >= 1（留空则默认使用 CPU 核心数）。")
    if args.chunk_size < 1:
        parser.error("--chunk-size 必须 >= 1。")
    if args.write_batch < 1:
        parser.error("--write-batch 必须 >= 1。")
    if args.max_pages is not None and args.max_pages < 1:
        parser.error("--max-pages 必须 >= 1（省略则为全部页数）。")
    if args.task_timeout < 0:
        parser.error("--task-timeout 必须 >= 0（0 禁用超时保护）。")
    if args.db_timeout < 0:
        parser.error("--db-timeout 必须 >= 0（0 禁用查询超时）。")
    if args.db_login_timeout < 1:
        parser.error("--db-login-timeout 必须 >= 1。")

    # 3. 计算输出表名、工作者数量、命名空间等
    schema = config.get("schema", "dbo")
    workers = args.workers or os.cpu_count() or 4
    # None = 所有命名空间；默认保持只处理文章（命名空间 0）。
    namespaces = None if args.all_namespaces else set(args.namespace or [0])

    output_tables = output_table_names_for_dump(
        dump_file,
        raw_table=config.get("table") or "wiki_latest",
        processed_table=args.processed_table,
        component_prefix=args.table_prefix,
        sections_table=args.sections_table,
        paragraphs_table=args.paragraphs_table,
        wtp_intermediate_table=args.wtp_intermediate_table,
        sentences_table=args.sentences_table,
        checkpoint_table=args.checkpoint_table,
    )
    processed_table = output_tables.processed
    sections_table = output_tables.sections
    paragraphs_table = output_tables.paragraphs
    wtp_intermediate_table = output_tables.wtp_intermediate
    sentences_table = output_tables.sentences
    component_table_version_suffix = output_tables.suffix

    # 4. 初始化各种写入器（原始表、处理表、组件表等）
    raw_config = {**config, "table": output_tables.raw}
    raw_writer = SQLServerWriter(
        raw_config,
        batch_size=args.write_batch,
        db_timeout=args.db_timeout,
        login_timeout=args.db_login_timeout,
    )
    processed_writer = engine.ProcessedTextWriter(
        config,
        schema,
        processed_table,
        batch_size=args.write_batch,
        db_timeout=args.db_timeout,
        login_timeout=args.db_login_timeout,
    )
    component_writer = engine.ComponentWriter(
        config,
        schema,
        batch_size=args.write_batch,
        table_prefix=output_tables.component_prefix,
        table_version_suffix=component_table_version_suffix,
        db_timeout=args.db_timeout,
        login_timeout=args.db_login_timeout,
    )
    sections_writer = engine.SectionsWriter(
        config,
        schema,
        sections_table,
        batch_size=args.write_batch,
        db_timeout=args.db_timeout,
        login_timeout=args.db_login_timeout,
    )
    paragraphs_writer = engine.ParagraphsWriter(
        config,
        schema,
        paragraphs_table,
        batch_size=args.write_batch,
        db_timeout=args.db_timeout,
        login_timeout=args.db_login_timeout,
    )
    wtp_intermediate_writer = engine.WtpIntermediateWriter(
        config,
        schema,
        wtp_intermediate_table,
        batch_size=args.write_batch,
        db_timeout=args.db_timeout,
        login_timeout=args.db_login_timeout,
    )
    sentences_writer = engine.SentencesWriter(
        config,
        schema,
        sentences_table,
        batch_size=args.write_batch,
        db_timeout=args.db_timeout,
        login_timeout=args.db_login_timeout,
    )
    sink = engine.FanoutWriter(
        processed_writer,
        component_writer,
        sections_writer,
        paragraphs_writer,
        wtp_intermediate_writer,
        sentences_writer,
    )

    # 打开 XML 源，准备 checkpoint（断点续传）
    checkpoint_store = None

    # 初始化 XML 数据源
    source = XmlBatchSource(
        dump_file,
        args.chunk_size,
        max_pages=args.max_pages,
        namespaces=namespaces,
    )
    source_name = str(dump_file)

    # 如果启用了断点续传，加载 checkpoint 并跳过已处理的页面
    if args.resume:
        source_id = source_id_for_path(dump_file)
        checkpoint_store = SQLServerCheckpointStore(
            config,
            schema,
            output_tables.checkpoint,
            source_id,
            db_timeout=args.db_timeout,
            login_timeout=args.db_login_timeout,
        )
        terminal_ids = checkpoint_store.load_terminal_ids(
            retry_errors=args.retry_errors
        )
        print(
            f"恢复：checkpoint_table={output_tables.checkpoint}, "
            f"source_id={source_id[:12]}, loaded_terminal={len(terminal_ids)}, "
            f"retry_errors={args.retry_errors}"
        )
        source = SkippingBatchSource(
            source,
            terminal_ids,
        )

    checkpoint = (
        engine.CheckpointCoordinator(checkpoint_store) if checkpoint_store else None
    )
    try:
        # 调用 engine._run_core 进行多进程并行解析
        articles, parse_failed = engine._run_core(
            source,
            sink,
            workers=workers,
            total=progress_total_for_source(is_xml=True, max_pages=args.max_pages),
            raw_writer=raw_writer,
            task_timeout=args.task_timeout or None,
            max_count=None,  # 限制在读取侧通过 --max-pages 完成
            key_column=engine.KEY_COLUMN,  # revision_id
            source_close=source.close,
            wtp_settings=wtp_settings,
            checkpoint=checkpoint,
            failure_logger=failure_logger,
        )
    finally:
        # 确保 checkpoint 数据库连接关闭
        if checkpoint_store is not None:
            checkpoint_store.close()

    # 解析结束后，打印日志统计（处理了多少篇、失败多少篇、写入了多少行）
    raw_table = output_tables.raw
    counts = component_writer.written
    comp_summary = ", ".join(f"{t}={counts[t]}" for t in engine.COMPONENT_TYPES)
    print(
        f"完成。articles={articles}, workers={workers}, "
        f"source={source_name}, raw_table={raw_table}, "
        f"processed_table={processed_table}"
    )
    print(
        f"  层次（行）：sections={sections_writer.written}, "
        f"paragraphs={paragraphs_writer.written}, "
        f"wtp_intermediate={wtp_intermediate_writer.written}, "
        f"sentences={sentences_writer.written}"
    )
    print(f"  组件（行）：{comp_summary}")
    if checkpoint_store is not None:
        print(
            f"  恢复：checkpoint_table={output_tables.checkpoint}, "
            f"source_id={checkpoint_store.source_id[:12]}, "
            f"skipped_terminal={source.skipped}"
        )
    if parse_failed:
        logging.warning("%s articles failed; see %s", parse_failed, run_log_path)


def main() -> None:
    parser = build_parser()
    session = start_run_log(Path(__file__).resolve().parent / "logs")
    configure_error_logging(session.path)
    original_stderr_fd = os.dup(2)
    try:
        print(f"日志：{session.path}")
        with redirect_stderr(session._log_file):
            os.dup2(session._log_file.fileno(), 2)
            try:
                args = parser.parse_args()
                _run(args, parser, session.write_parse_failure, session.path)
            except SystemExit:
                raise
            except KeyboardInterrupt:
                logging.warning("Interrupted by user")
                raise SystemExit(130) from None
            except BaseException:
                logging.exception("Fatal parser failure")
                raise SystemExit(1) from None
    finally:
        logging.shutdown()
        session.close()
        os.dup2(original_stderr_fd, 2)
        os.close(original_stderr_fd)


if __name__ == "__main__":
    main()
