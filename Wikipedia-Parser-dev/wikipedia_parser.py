#!/usr/bin/env python3
"""
组合流水线：一次性读取 XML 转储 -> 分叉到两个输出。

  XML dump ──单遍流──┬──▶ latest    (原始副本，始终写入)
                     └──▶ multiprocess mwparserfromhell 解析──▶ processed
                                                                   component_* 表
                                                                   sections / paragraph / wtp_intermediate / sentence

与两步法（"先 XML->DB，然后读取 DB->process"）相比，
此流程只读取一次 350MB 的转储文件，并且只解压一次。原始写入
和组件写入沿着两条路径**并行运行**。

- 原始表（latest）具有固定结构，由 `xml_source.SQLServerWriter` 写入。
- 解析链（sections -> 每 section 组件 -> paragraphs -> sentences）写入
  `processed`、七个组件表（infobox/table/wikilinks/external_links/
  file/image/ref）以及三个层次表
  （sections / paragraph / wtp_intermediate / sentence）。
- 编排核心 `engine._run_core`：批处理源（XML 流）+ 处理池解析 + 双重写入，
  包括反压/超时/首错/清理加固。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable

# pipeline/下的模块：XML 侧（原始写入器 + 流式批处理源）和
# 处理引擎。将其放在 sys.path 第一位，这样多进程 spawned 的 worker
# 也可以通过模块名重新导入 engine（其中包含 process_batch）。
sys.path.insert(0, str(Path(__file__).resolve().parent / "pipeline"))
import engine  # noqa: E402
from checkpoint import SQLServerCheckpointStore, source_id_for_path  # noqa: E402
from pipeline_config import (  # noqa: E402
    load_pipeline_config,
    output_table_names_for_dump,
    resolve_dump_file,
)
from run_logging import start_run_log  # noqa: E402
from xml_source import (  # noqa: E402
    SQLServerBatchSource,
    SQLServerWriter,
    SkippingBatchSource,
    XmlBatchSource,
)


def progress_total_for_source(*, is_xml: bool, max_pages: int | None) -> int | None:
    """为源类型返回一个可信的百分比分母。"""
    # XML 的 max_pages 限制物理 <page> 元素。命名空间过滤和
    # 逻辑恢复过滤稍后进行，因此它不是 Read/Parse/Write/Raw
    # 处理的计数。让这些流进度条只计数和速率。
    return None if is_xml else max_pages


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="单遍 XML 转储读取：写入原始、处理、组件、WTP 和层次表。XML 输出表名从转储文件名获得 _[YYYYMMDD] 后缀。"
    )
    p.add_argument(
        "dump_file", type=Path, nargs="?",
        help="Wikipedia 转储路径 (*.xml.bz2 或 *.xml)。与 --source-table 一起使用时省略。",
    )
    p.add_argument(
        "--config", type=Path, required=True,
        help="包含源、sqlserver 和 wtp 设置的统一配置 JSON 文件。",
    )
    p.add_argument(
        "--source-table", default=None,
        help="而不是 XML 转储，从现有 SQL Server 行读取（例如 latest）。",
    )
    p.add_argument(
        "--processed-table",
        default="wiki_processed",
        help="处理表名称（每篇文章一行；text_process = 用组件 id 替换组件的 wikitext）。默认 wiki_processed。",
    )
    p.add_argument(
        "--table-prefix",
        default="wiki_component",
        help="组件表基础名称的前缀；XML 输入写入 <prefix>_<type>_[YYYYMMDD]。默认 wiki_component。原始基础名称来自 config.table（默认 wiki_latest）。",
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
    p.add_argument("--workers", type=int, default=None, help="解析进程数；默认使用 CPU 核心数。")
    p.add_argument("--chunk-size", type=int, default=500, help="每批页数。")
    p.add_argument("--write-batch", type=int, default=500, help="两个表的批量 upsert 大小。")
    p.add_argument("--max-pages", type=int, default=None, help="要处理的最大页数；省略则为全部。")
    p.add_argument(
        "--namespace", type=int, action="append", default=None,
        help="只处理这些命名空间（可重复）。默认：0（文章）。使用 --all-namespaces 处理所有。",
    )
    p.add_argument("--all-namespaces", action="store_true", help="处理所有命名空间（覆盖 --namespace）。")
    p.add_argument(
        "--task-timeout", type=float, default=300.0,
        help="每批解析的最大等待时间（秒）；防止卡住/极慢的 worker 阻塞主流程；0 禁用。默认 300。",
    )
    p.add_argument(
        "--db-timeout", type=float, default=300.0,
        help="DB 查询超时（秒，两个表的读取/写入）；防止网络抖动/停滞服务器导致无限阻塞；0 禁用。默认 300。",
    )
    p.add_argument("--db-login-timeout", type=float, default=60.0, help="DB 连接/登录超时（秒）。默认 60。")
    p.add_argument(
        "--resume", action=argparse.BooleanOptionalAction, default=True,
        help="对于 XML 输入，跳过 checkpoint 表中记录的终端修订版。默认：启用。",
    )
    p.add_argument(
        "--retry-errors", action="store_true",
        help="与 XML 恢复一起，重新处理 completed_with_errors 修订版。",
    )
    p.add_argument(
        "--checkpoint-table", default="wiki_parse_checkpoint",
        help="持久化 XML 恢复状态的表基础名称；XML 输入附加 _[YYYYMMDD]。默认 wiki_parse_checkpoint。",
    )
    return p


def _run(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    failure_logger: Callable[[str], None] | None = None,
) -> None:
    import os

    if not args.config.exists():
        parser.error(f"--config 文件不存在：{args.config}")
    unified_config = load_pipeline_config(args.config)
    config = unified_config.sqlserver
    wtp_settings = unified_config.wtp
    configured_dump_file = unified_config.dump_file

    dump_file = resolve_dump_file(
        cli_dump_file=args.dump_file,
        source_table=args.source_table,
        configured_dump_file=configured_dump_file,
    )

    # 参数边界验证：尽早给出友好的错误。
    if bool(dump_file) == bool(args.source_table):
        parser.error("提供恰好一个输入：dump_file 或 --source-table")
    if dump_file is not None and not dump_file.exists():
        parser.error(f"转储文件不存在：{dump_file}")
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

    schema = config.get("schema", "dbo")
    workers = args.workers or os.cpu_count() or 4
    # None = 所有命名空间；默认保持只处理文章（命名空间 0）。
    namespaces = None if args.all_namespaces else set(args.namespace or [0])

    output_tables = None
    if dump_file is not None:
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
    processed_table = output_tables.processed if output_tables else args.processed_table
    sections_table = output_tables.sections if output_tables else args.sections_table
    paragraphs_table = output_tables.paragraphs if output_tables else args.paragraphs_table
    wtp_intermediate_table = (
        output_tables.wtp_intermediate if output_tables else args.wtp_intermediate_table
    )
    sentences_table = output_tables.sentences if output_tables else args.sentences_table
    component_table_version_suffix = output_tables.suffix if output_tables else ""

    # XML 导入保留原始表的分叉输出。SQL 源已经是原始
    # 数据，因此故意不写回到 latest。
    raw_writer = None
    if dump_file is not None:
        raw_config = {**config, "table": output_tables.raw}
        raw_writer = SQLServerWriter(
            raw_config,
            batch_size=args.write_batch,
            db_timeout=args.db_timeout,
            login_timeout=args.db_login_timeout,
        )
    processed_writer = engine.ProcessedTextWriter(
        config, schema, processed_table,
        batch_size=args.write_batch,
        db_timeout=args.db_timeout, login_timeout=args.db_login_timeout,
    )
    component_writer = engine.ComponentWriter(
        config, schema, batch_size=args.write_batch,
        table_prefix=(output_tables.component_prefix if output_tables else args.table_prefix),
        table_version_suffix=component_table_version_suffix,
        db_timeout=args.db_timeout, login_timeout=args.db_login_timeout,
    )
    sections_writer = engine.SectionsWriter(
        config, schema, sections_table,
        batch_size=args.write_batch,
        db_timeout=args.db_timeout, login_timeout=args.db_login_timeout,
    )
    paragraphs_writer = engine.ParagraphsWriter(
        config, schema, paragraphs_table,
        batch_size=args.write_batch,
        db_timeout=args.db_timeout, login_timeout=args.db_login_timeout,
    )
    wtp_intermediate_writer = engine.WtpIntermediateWriter(
        config, schema, wtp_intermediate_table,
        batch_size=args.write_batch,
        db_timeout=args.db_timeout, login_timeout=args.db_login_timeout,
    )
    sentences_writer = engine.SentencesWriter(
        config, schema, sentences_table,
        batch_size=args.write_batch,
        db_timeout=args.db_timeout, login_timeout=args.db_login_timeout,
    )
    sink = engine.FanoutWriter(
        processed_writer, component_writer,
        sections_writer, paragraphs_writer, wtp_intermediate_writer, sentences_writer,
    )

    checkpoint_store = None
    if dump_file is not None:
        source = XmlBatchSource(
            dump_file, args.chunk_size, max_pages=args.max_pages,
            namespaces=namespaces,
        )
        source_name = str(dump_file)
        if args.resume:
            source_id = source_id_for_path(dump_file)
            checkpoint_store = SQLServerCheckpointStore(
                config, schema, output_tables.checkpoint, source_id,
                db_timeout=args.db_timeout, login_timeout=args.db_login_timeout,
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
    else:
        source = SQLServerBatchSource(
            config, schema, args.source_table, args.chunk_size,
            max_pages=args.max_pages, namespaces=namespaces,
            db_timeout=args.db_timeout, login_timeout=args.db_login_timeout,
        )
        source_name = f"{schema}.{args.source_table}"

    checkpoint = engine.CheckpointCoordinator(checkpoint_store) if checkpoint_store else None
    try:
        articles, parse_failed = engine._run_core(
            source,
            sink,
            workers=workers,
            total=progress_total_for_source(
                is_xml=dump_file is not None,
                max_pages=args.max_pages,
            ),
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
        if checkpoint_store is not None:
            checkpoint_store.close()

    raw_table = output_tables.raw if output_tables else config.get("table", "wiki_latest")
    counts = component_writer.written
    comp_summary = ", ".join(f"{t}={counts[t]}" for t in engine.COMPONENT_TYPES)
    print(
        f"完成。articles={articles}, parse_failed={parse_failed}, workers={workers}, "
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
        print(f"警告：{parse_failed} 篇文章解析失败（参见上方的 [parse-fail] 日志）。")


def main() -> None:
    parser = build_parser()
    session = start_run_log(Path(__file__).resolve().parent / "logs")
    try:
        print(f"日志：{session.path}")
        args = parser.parse_args()
        _run(args, parser, session.write_parse_failure)
    finally:
        session.close()


if __name__ == "__main__":
    main()
