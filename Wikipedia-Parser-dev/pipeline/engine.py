#!/usr/bin/env python3
"""
处理引擎：批量读取 -> 多进程解析 -> 写入处理结果表
（也可以同时将数据分流写入原始数据表）。

由 wikipedia_parser.py 调用。核心函数 `_run_core(batches, processed_writer,
raw_writer=...)` 与批次来源解耦：批次来自 XML 数据流，由进程池使用
mwparserfromhell 解析（这是 CPU 密集型工作，可绕过 GIL）；读取、解析、写入
（以及原始数据写入）分别由独立线程驱动，并各自显示 tqdm 速度条。引擎还包含
背压控制、工作进程超时处理、首个错误优先机制，以及不会掩盖根因的清理流程，
并会记录解析失败。

参考实现：对每篇 namespace 为 0 的文章依次执行：
  1. extract_sections（section_extractor：按 == 标题 == 切分）
  2. 对每个章节调用 extract_and_templatize
     （8 种组件：infobox / table / independent_template / wikilinks /
      external_links / file / image / ref；跳过目录匹配章节并保留原文）
  3. extract_paragraphs（paragraph_extractor：按空行切分并生成子章节目录）
  4. extract_sentences（sentence_extractor：使用 sentencex 并处理 wikilink/列表）
每种组件通过 ComponentWriter 写入独立数据表；章节、段落和句子则分别通过
SectionsWriter、ParagraphsWriter 和 SentencesWriter 写入，每条记录各占一行。
`_run_core` 保持通用；替换提取器和写入端即可改变输出内容。

"""

from __future__ import annotations

from collections.abc import Sequence
import logging
import queue
import sys
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

import pymssql
from tqdm import tqdm

# 层级提取功能（独立复制自 API-Parser 的 lib 模块，并保持功能一致）：
# 章节 -> 段落 -> 句子。
from section_extractor import extract_sections  # noqa: E402
from paragraph_extractor import extract_paragraphs  # noqa: E402
from sentence_extractor import extract_sentences  # noqa: E402
from component_extractor import (  # noqa: E402
    COMPONENT_TYPES,
    extract_and_templatize,
    restore_component_sources,
)
from wtp_integration import (  # noqa: E402
    analyze_wikitext,
    begin_page,
    expand_to_text,
    initialize_wtp_worker,
)

# 主键：原始数据和处理结果都使用 revision_id；超时定位和日志记录也依赖它。
KEY_COLUMN = "revision_id"


@dataclass
class BatchResult:
    """process_batch 的返回值：待写入记录和解析失败详情，用于观察数据质量。"""

    records: list[dict[str, Any]]        # 待写入的记录；包含失败行，失败行的 parse_error 非空
    failures: list[tuple[Any, str]]      # (源记录主键值, 错误信息)，用于统计和记录日志
    paragraphs_total: int = 0
    paragraphs_succeeded: int = 0
    paragraphs_failed: int = 0
    wtp_errors: int = 0


@dataclass
class ParagraphRunStats:
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    wtp_errors: int = 0


# =========================================================================== #
# 解析实现相关定义（以下为可整体替换的参考实现）。
#
# 每篇 namespace 为 0 的文章会生成：
#   * text_process -- 将维基文本中的每个组件替换为对应 ID 后得到的文本
#   * components    -- {type: [(component_id, component_text), ...]}，按文档顺序排列
#   * sections / paragraphs / sentences -- 三种层级记录列表
# text_process 由 ProcessedTextWriter 写入，组件由 ComponentWriter 写入，
# 层级记录由 SectionsWriter / ParagraphsWriter / SentencesWriter 写入。
# =========================================================================== #
# 这些章节仍会提取组件，但跳过 WTP 展开和句子切分。
_SKIP_WTP_TOC_ROOTS = frozenset({
    "references",
    "other websites",
    "related pages",
    "more reading",
    "category",
    "external links",
    "footnotes",
    "notes"
})

def should_skip_wtp_for_toc(toc: str | None) -> bool:
    """执行should跳过wtpfor目录的处理逻辑。"""
    if not toc:
        return False
    root = toc.split(":", 1)[0].strip().casefold()
    return root in _SKIP_WTP_TOC_ROOTS

def _section_part(section: Any) -> str:
    """text_process 的一个拼接单元：原始 ``== Title ==`` 标题行 + 节体。

    extract_sections 把标题行当作分隔符消费掉（只留在 Section.toc），这里补回，
    使 text_process 能定位节边界。无标题节（Summary）没有 heading，原样返回。
    """
    heading = getattr(section, "heading", None)
    if not heading:
        return section.text
    body = (section.text or "").strip()
    return f"{heading}\n\n{body}" if body else heading


def build_bundle(
    row: Mapping[str, Any], expand_timeout: float = 15.0
) -> tuple[dict[str, Any], str | None, ParagraphRunStats]:
    """执行构建文章结果的处理逻辑。"""
    stats = ParagraphRunStats()
    try:
        if row.get("model") in (None, "wikitext"):
            sections = extract_sections(row.get("content"), row["page_id"])
            components = {t: [] for t in COMPONENT_TYPES}
            component_sources: dict[str, str] = {}
            # 使用 MWP 生成的组件占位符作为 WTP 输入。这里刻意不按模板过滤，
            # 不跳过章节，也不删除格式块。
            for section in sections:
                transformed, _ = extract_and_templatize(
                    section.text, row["page_id"], components, component_sources
                )
                section.text = transformed
            text_process = "\n\n".join(_section_part(section) for section in sections)
            paragraphs = extract_paragraphs(sections, row["page_id"])

            # 遇到参考资料或链接类目录根节点后，该段及文章中后续所有段落都只保留为段落。
            # 这些段落的 toc 可能带有子章节后缀，例如 ``References:Books``。
            wtp_paragraphs = []
            skip_remaining = False
            for paragraph in paragraphs:
                transformed_wikitext = paragraph.text
                paragraph.raw_wikitext = restore_component_sources(
                    transformed_wikitext, component_sources
                )
                paragraph.wtp_skipped = (
                    skip_remaining or should_skip_wtp_for_toc(paragraph.toc)
                )
                if paragraph.wtp_skipped:
                    skip_remaining = True
                    continue
                wtp_paragraphs.append(paragraph)

            # 先建立页面上下文，再分别对上面未跳过的段落执行 MWP/WTP 处理。
            if wtp_paragraphs:
                begin_page(str(row["page_title"]))
            for paragraph in wtp_paragraphs:
                transformed_wikitext = paragraph.text
                _template_count, mwp_error = analyze_wikitext(transformed_wikitext)
                final_text, expanded_wikitext, wtp_error = expand_to_text(
                    transformed_wikitext, expand_timeout
                )
                paragraph.wtp_input = transformed_wikitext
                paragraph.expanded_wikitext = expanded_wikitext
                paragraph.text = final_text
                paragraph.parse_error = "; ".join(
                    issue for issue in (mwp_error, wtp_error) if issue
                ) or None
                stats.total += 1
                if paragraph.parse_error:
                    stats.failed += 1
                    stats.wtp_errors += int(wtp_error is not None)
                else:
                    stats.succeeded += 1

            sentences = extract_sentences(wtp_paragraphs, row["page_id"])
            error = (
                f"paragraph_errors={stats.failed}; wtp_errors={stats.wtp_errors}"
                if stats.failed else None
            )
        else:
            text_process = str(row.get("content") or "")
            sections, paragraphs, sentences = [], [], []
            components = {t: [] for t in COMPONENT_TYPES}
            error = None
    except Exception as exc:
        text_process = ""
        sections, paragraphs, sentences = [], [], []
        components = {t: [] for t in COMPONENT_TYPES}
        error = f"{type(exc).__name__}: {exc}"
    bundle = {
        "revision_id": row["revision_id"],
        "page_id": row["page_id"],
        "page_title": row["page_title"],
        "namespace": row["namespace"],
        "text_process": text_process,
        "parse_error": error,
        "components": components,
        "sections": sections,
        "paragraphs": paragraphs,
        "sentences": sentences,
    }
    return bundle, error, stats


def process_batch(
    rows: list[dict[str, Any]], expand_timeout: float = 15.0
) -> BatchResult:
    """执行处理批次的处理逻辑。"""
    records: list[dict[str, Any]] = []
    failures: list[tuple[Any, str]] = []
    stats = ParagraphRunStats()
    for row in rows:
        bundle, error, row_stats = build_bundle(row, expand_timeout)
        records.append(bundle)
        stats.total += row_stats.total
        stats.succeeded += row_stats.succeeded
        stats.failed += row_stats.failed
        stats.wtp_errors += row_stats.wtp_errors
        if error is not None:
            failures.append((row.get(KEY_COLUMN), error))
    return BatchResult(
        records=records,
        failures=failures,
        paragraphs_total=stats.total,
        paragraphs_succeeded=stats.succeeded,
        paragraphs_failed=stats.failed,
        wtp_errors=stats.wtp_errors,
    )


# =========================================================================== #
# 通用引擎（通常无需修改）：配置、连接和任务编排。
# =========================================================================== #
def quote_ident(name: str) -> str:
    """执行转义标识符的处理逻辑。"""
    if not name:
        raise ValueError("Identifier must be non-empty.")
    return "[" + name.replace("]", "]]") + "]"


def qualified_name(schema: str, table: str) -> str:
    """执行qualifiedname的处理逻辑。"""
    return f"{quote_ident(schema)}.{quote_ident(table)}"


def unique_constraint_name(table: str, key_suffix: str) -> str:
    """执行uniqueconstraintname的处理逻辑。"""
    return quote_ident(f"UQ_{table}_{key_suffix}")


def drop_legacy_columns_sql(
    qualified: str, object_name: str, columns: Sequence[str]
) -> str:
    """执行drop旧版columnssql的处理逻辑。"""
    statements: list[str] = []
    for index, column in enumerate(columns):
        constraint_var = f"@default_constraint_{index}"
        drop_sql_var = f"@drop_default_sql_{index}"
        statements.append(f"""
        IF COL_LENGTH(N'{object_name}', N'{column}') IS NOT NULL
        BEGIN
            DECLARE {constraint_var} SYSNAME;
            DECLARE {drop_sql_var} NVARCHAR(MAX);
            SELECT {constraint_var} = dc.name
            FROM sys.default_constraints AS dc
            INNER JOIN sys.columns AS c
                ON c.object_id = dc.parent_object_id
               AND c.column_id = dc.parent_column_id
            WHERE dc.parent_object_id = OBJECT_ID(N'{object_name}')
              AND c.name = N'{column}';
            IF {constraint_var} IS NOT NULL
            BEGIN
                SET {drop_sql_var} = N'ALTER TABLE {qualified} DROP CONSTRAINT '
                    + QUOTENAME({constraint_var});
                EXEC sys.sp_executesql {drop_sql_var};
            END
            ALTER TABLE {qualified} DROP COLUMN [{column}];
        END
        """)
    return "\n".join(statements)


def terminate_workers(executor: ProcessPoolExecutor) -> None:
    """执行terminateworkers的处理逻辑。"""
    procs = getattr(executor, "_processes", None)
    if not procs:
        return
    for p in list(procs.values()):
        try:
            p.terminate()
        except Exception:
            pass


def connect(
    config: dict[str, Any],
    autocommit: bool,
    db_timeout: float = 300.0,
    login_timeout: float = 60.0,
) -> Any:
    # timeout 是查询超时时间（秒），防止 cur.execute()/executemany() 因网络抖动或服务器无响应而一直阻塞。
    # task_timeout 只限制解析时间，不限制数据库读写；设为 0 表示不限制。
    # login_timeout 是连接/登录超时时间（秒），防止建立连接时一直等待。
    """执行connect的处理逻辑。"""
    return pymssql.connect(
        server=config["host"],
        port=int(config.get("port", 1433)),
        user=config["user"],
        password=config["password"],
        database=config["database"],
        charset="utf8",
        autocommit=autocommit,
        timeout=int(db_timeout),
        login_timeout=int(login_timeout),
    )


# --------------------------------------------------------------------------- #
# 处理文本写入器：每篇文章在 processed 表中占一行，并按 revision_id 执行可重复的 MERGE upsert。
# text_process 是组件已替换为对应 ID 的维基文本。upsert 使用分块的多行 MERGE 语句，
# 每个数据块只需一次数据库往返，而不是每行往返一次。
# --------------------------------------------------------------------------- #
class ProcessedTextWriter:
    _COLS = ("revision_id", "page_id", "page_title", "namespace", "text_process", "parse_error")

    def __init__(
        self,
        config: dict[str, Any],
        schema: str,
        table: str = "wiki_processed",
        batch_size: int = 500,
        db_timeout: float = 300.0,
        login_timeout: float = 60.0,
    ) -> None:
        """初始化对象所需的状态和资源。"""
        self.batch_size = batch_size
        self.qualified = qualified_name(schema, table)
        self.object_name = self.qualified
        self._merge_sql = self._build_merge_sql()
        self._buffer: list[tuple[Any, ...]] = []
        self._conn = connect(
            config, autocommit=False, db_timeout=db_timeout, login_timeout=login_timeout
        )
        self._create_table()

    def _create_table(self) -> None:
        """执行创建数据表的处理逻辑。"""
        with self._conn.cursor() as cur:
            cur.execute(f"""
            IF OBJECT_ID(N'{self.qualified}', N'U') IS NULL
            BEGIN
                CREATE TABLE {self.qualified} (
                    [revision_id] BIGINT NOT NULL PRIMARY KEY,
                    [page_id] BIGINT NOT NULL,
                    [page_title] NVARCHAR(512) NOT NULL,
                    [namespace] INT NOT NULL,
                    [text_process] NVARCHAR(MAX) NULL,
                    [parse_error] NVARCHAR(MAX) NULL
                );
            END
            {drop_legacy_columns_sql(self.qualified, self.object_name, ('processed_at',))}
            """)
        self._conn.commit()

    def add_rows(self, bundles: list[dict[str, Any]]) -> None:
        """接收一批数据并追加到内部缓冲区。"""
        for b in bundles:
            self._buffer.append(tuple(b[c] for c in self._COLS))
        if len(self._buffer) >= self.batch_size:
            self.flush()

    def _build_merge_sql(self) -> str:
        """执行构建mergesql的处理逻辑。"""
        cols = ", ".join(self._COLS)
        set_clause = ",\n                ".join(f"{c} = source.{c}" for c in self._COLS)
        source_cols = ", ".join(f"source.{c}" for c in self._COLS)
        return f"""
        MERGE {self.qualified} AS target
        USING (VALUES {{values}}) AS source ({cols})
        ON target.revision_id = source.revision_id
        WHEN MATCHED THEN
            UPDATE SET
                {set_clause}
        WHEN NOT MATCHED THEN
            INSERT ({cols})
            VALUES ({source_cols});
        """

    def flush(self) -> None:
        """将当前缓冲的数据写入目标位置。"""
        if not self._buffer:
            return
        # 使用分块的多行 MERGE，每个数据块只需一次数据库往返；
        # 控制块大小，使每条语句的参数数低于 SQL Server 的 2100 个上限。
        rows_per_stmt = max(1, 2000 // len(self._COLS))
        row = "(" + ", ".join(["%s"] * len(self._COLS)) + ")"
        try:
            with self._conn.cursor() as cur:
                for i in range(0, len(self._buffer), rows_per_stmt):
                    chunk = self._buffer[i:i + rows_per_stmt]
                    cur.execute(
                        self._merge_sql.format(values=",\n            ".join([row] * len(chunk))),
                        [v for r in chunk for v in r],
                    )
            self._conn.commit()
            self._buffer.clear()
        except Exception:
            self._conn.rollback()
            raise

    def close(self) -> None:
        """刷新未写入的数据并释放相关资源。"""
        self.flush()
        self._conn.close()


# --------------------------------------------------------------------------- #
# 组件写入器：每种组件类型对应一张 component_<type> 表。
# 行记录预先带有 <type>_id（由工作进程按页面分配，与 text_process 中嵌入的 ID 一致）
# 以及所属页面的 page_id。
# 每次 flush 都按 page_id 先删后插（层级写入器则按 revision_id 操作）：先删除自上次
# flush 以来处理过的所有页面的记录，包括本次解析没有生成该类型组件的页面。这样可以
# 自动清除旧任务遗留的行，以及解析流程变更后不再生成或生成数量减少的陈旧记录。
#
# 每张表都对 <type>_id 设置 UNIQUE 约束，重复 ID 会在插入时明确报错，避免静默累积重复行；
# 同时为 page_id 建立索引，以加速 flush 时的 DELETE。表在启动时新建，因此这些约束和索引
# 都在 CREATE TABLE 语句中定义。
# --------------------------------------------------------------------------- #
class ComponentWriter:
    def __init__(
        self,
        config: dict[str, Any],
        schema: str,
        batch_size: int = 500,
        table_prefix: str = "wiki_component",
        table_version_suffix: str = "",
        db_timeout: float = 300.0,
        login_timeout: float = 60.0,
    ) -> None:
        """初始化对象所需的状态和资源。"""
        self.batch_size = batch_size
        self._table_names = {
            t: f"{table_prefix}_{t}{table_version_suffix}" for t in COMPONENT_TYPES
        }
        self._tables = {t: qualified_name(schema, name) for t, name in self._table_names.items()}
        self._object_names = dict(self._tables)
        self._buffers: dict[str, list[tuple[int, str, str]]] = {t: [] for t in COMPONENT_TYPES}
        # 记录自上次 flush 以来处理过的页面，作为各组件表 DELETE 的键。
        # 批次中的每个页面都会记录，即使解析没有生成某种组件；否则组件已完全消失的页面
        # 会残留旧记录，无法被删除。
        self._pending_pages: set[int] = set()
        self.written: dict[str, int] = {t: 0 for t in COMPONENT_TYPES}
        self._conn = connect(
            config, autocommit=False, db_timeout=db_timeout, login_timeout=login_timeout
        )
        self._create_tables()

    def _create_tables(self) -> None:
        """执行创建tables的处理逻辑。"""
        with self._conn.cursor() as cur:
            for t, q in self._tables.items():
        # 约束和索引名称在数据库范围内唯一，因此名称中包含表名。
                uq = quote_ident(f"UQ_{self._table_names[t]}_id")
                ix = quote_ident(f"IX_{self._table_names[t]}_page")
                cur.execute(f"""
                IF OBJECT_ID(N'{q}', N'U') IS NULL
                BEGIN
                    CREATE TABLE {q} (
                        [id] BIGINT IDENTITY(1,1) PRIMARY KEY,
                        [page_id] BIGINT NOT NULL,
                        {quote_ident(t + '_id')} NVARCHAR(128) NOT NULL,
                        {quote_ident(t + '_text')} NVARCHAR(MAX) NULL,
                        CONSTRAINT {uq} UNIQUE ({quote_ident(t + '_id')})
                    );
                    CREATE INDEX {ix} ON {q} ([page_id]);
                END
                {drop_legacy_columns_sql(q, self._object_names[t], ('create_time',))}
                """)
        self._conn.commit()

    def add_rows(self, bundles: list[dict[str, Any]]) -> None:
        """接收一批数据并追加到内部缓冲区。"""
        for b in bundles:
            self._pending_pages.add(b["page_id"])
            for t, items in b["components"].items():
        # items 中的元素为 (component_id, text)；从 bundle 中取出所属 page_id 并添加到记录前面，
        # 工作进程侧无需变更。
                self._buffers[t].extend((b["page_id"], cid, text) for cid, text in items)
        if any(len(buf) >= self.batch_size for buf in self._buffers.values()):
            self.flush()

    def flush(self) -> None:
        """将当前缓冲的数据写入目标位置。"""
        if not any(self._buffers.values()) and not self._pending_pages:
            return
        try:
            for t, buf in self._buffers.items():
            # 在一个事务中先删后插：删除自上次 flush 以来处理过的每个页面的所有记录，
            # 包括本次解析没有生成该类型组件的页面，从而完整替换这些页面的组件集合，
            # 并清除陈旧记录。
                _delete_by_keys(
                    self._conn, self._tables[t], "page_id",
                    self._pending_pages,
                )
                if buf:
                    _insert_multirow(
                        self._conn, self._tables[t],
                        ["page_id", t + "_id", t + "_text"],
                        buf,
                    )
            self._conn.commit()
            # 只有成功提交后才更新计数器并清空缓冲区。失败时 rollback 会撤销所有类型的写入，
            # 因此缓冲区必须保留，以便 close() 重试；`written` 也不能统计未提交的行。
            for t, buf in self._buffers.items():
                if buf:
                    self.written[t] += len(buf)
                    buf.clear()
            self._pending_pages.clear()
        except Exception:
            self._conn.rollback()
            raise

    def close(self) -> None:
        """刷新未写入的数据并释放相关资源。"""
        self.flush()
        self._conn.close()


# --------------------------------------------------------------------------- #
# 层级写入器：sections / paragraphs / sentences 分别按章节、段落和句子一行写入。
# 每次 flush 都像 API-Parser 一样先删后插：删除批次中各 revision 的旧记录再重新写入，
# 从而自动清理旧任务或解析流程变更后遗留的陈旧详情行。每个 revision_id 在一次运行中
# 只会出现在一个 flush 批次里，因此删除操作不会移除本次事务中没有重新写入的记录。
# --------------------------------------------------------------------------- #
def _delete_by_keys(
    conn: Any,
    qualified: str,
    column: str,
    keys: Iterable[Any],
    chunk_size: int = 500,
) -> None:
    """执行deletebykeys的处理逻辑。"""
    keys = sorted(set(keys))
    if not keys:
        return
    with conn.cursor() as cur:
        for i in range(0, len(keys), chunk_size):
            chunk = keys[i:i + chunk_size]
            placeholders = ", ".join(["%s"] * len(chunk))
            cur.execute(
                f"DELETE FROM {qualified} WHERE [{column}] IN ({placeholders})", chunk
            )


def _insert_multirow(
    conn: Any,
    qualified: str,
    columns: Sequence[str],
    rows: list[tuple[Any, ...]],
) -> None:
    """执行insertmultirow的处理逻辑。"""
    ncols = len(columns)
    rows_per_stmt = max(1, 2000 // ncols)
    col_list = ", ".join(f"[{c}]" for c in columns)
    row = "(" + ", ".join(["%s"] * ncols) + ")"
    with conn.cursor() as cur:
        for i in range(0, len(rows), rows_per_stmt):
            chunk = rows[i:i + rows_per_stmt]
            cur.execute(
                f"INSERT INTO {qualified} ({col_list}) "
                f"VALUES {', '.join([row] * len(chunk))};",
                [v for r in chunk for v in r],
            )


class SectionsWriter:
    """sections 写入器：每次 flush 先删后插，以清除陈旧记录。"""

    _COLS = (
        "revision_id", "page_id", "page_title", "section_no", "toc",
        "raw_text", "text",
    )

    def __init__(
        self,
        config: dict[str, Any],
        schema: str,
        table: str = "wiki_sections",
        batch_size: int = 500,
        db_timeout: float = 300.0,
        login_timeout: float = 60.0,
    ) -> None:
        """初始化对象所需的状态和资源。"""
        self.batch_size = batch_size
        self.qualified = qualified_name(schema, table)
        self.object_name = self.qualified
        self._unique_constraint = unique_constraint_name(table, "rev_secno")
        self._buffer: list[tuple[Any, ...]] = []
        self._pending_revision_ids: set[Any] = set()
        self.written = 0
        self._conn = connect(
            config, autocommit=False, db_timeout=db_timeout, login_timeout=login_timeout
        )
        self._create_table()

    def _create_table(self) -> None:
        """执行创建数据表的处理逻辑。"""
        with self._conn.cursor() as cur:
            cur.execute(f"""
            IF OBJECT_ID(N'{self.qualified}', N'U') IS NULL
            BEGIN
                CREATE TABLE {self.qualified} (
                    [id] BIGINT IDENTITY(1,1) PRIMARY KEY,
                    [revision_id] BIGINT NOT NULL,
                    [page_id] BIGINT NOT NULL,
                    [page_title] NVARCHAR(512) NOT NULL,
                    [section_no] INT NOT NULL,
                    [toc] NVARCHAR(MAX) NULL,
                    [raw_text] NVARCHAR(MAX) NULL,
                    [text] NVARCHAR(MAX) NULL,
                    CONSTRAINT {self._unique_constraint} UNIQUE ([revision_id], [section_no])
                );
            END
            IF COL_LENGTH(N'{self.object_name}', N'page_title') IS NULL
                ALTER TABLE {self.qualified} ADD [page_title] NVARCHAR(512) NULL;
            {drop_legacy_columns_sql(self.qualified, self.object_name, ('processed_at',))}
            """)
        self._conn.commit()

    def add_rows(self, bundles: list[dict[str, Any]]) -> None:
        """接收一批数据并追加到内部缓冲区。"""
        for b in bundles:
            self._pending_revision_ids.add(b["revision_id"])
            for s in b["sections"]:
                self._buffer.append(
                    (b["revision_id"], b["page_id"], b["page_title"],
                     s.section_no, s.toc, s.raw_text, s.text)
                )
        if (len(self._buffer) >= self.batch_size
                or len(self._pending_revision_ids) >= self.batch_size):
            self.flush()

    def flush(self) -> None:
        """将当前缓冲的数据写入目标位置。"""
        if not self._buffer and not self._pending_revision_ids:
            return
        try:
            # 在一个事务中先删后插；空结果也会清理已处理 revisions 的旧行。
            _delete_by_keys(
                self._conn, self.qualified, "revision_id", self._pending_revision_ids
            )
            if self._buffer:
                _insert_multirow(self._conn, self.qualified, self._COLS, self._buffer)
            self._conn.commit()
            self.written += len(self._buffer)
            self._buffer.clear()
            self._pending_revision_ids.clear()
        except Exception:
            self._conn.rollback()
            raise

    def close(self) -> None:
        """刷新未写入的数据并释放相关资源。"""
        self.flush()
        self._conn.close()


class ParagraphsWriter:
    """paragraph 写入器：每次 flush 先删后插，以清除陈旧记录。"""

    _COLS = (
        "revision_id", "page_id", "page_title", "section_no", "paragraph_no",
        "toc", "raw_wikitext", "text", "parse_error",
    )

    def __init__(
        self,
        config: dict[str, Any],
        schema: str,
        table: str = "wiki_paragraph",
        batch_size: int = 500,
        db_timeout: float = 300.0,
        login_timeout: float = 60.0,
    ) -> None:
        """初始化对象所需的状态和资源。"""
        self.batch_size = batch_size
        self.qualified = qualified_name(schema, table)
        self.object_name = self.qualified
        self._unique_constraint = unique_constraint_name(table, "rev_secno_pno")
        self._buffer: list[tuple[Any, ...]] = []
        self._pending_revision_ids: set[Any] = set()
        self.written = 0
        self._conn = connect(
            config, autocommit=False, db_timeout=db_timeout, login_timeout=login_timeout
        )
        self._create_table()

    def _create_table(self) -> None:
        """执行创建数据表的处理逻辑。"""
        with self._conn.cursor() as cur:
            cur.execute(f"""
            IF OBJECT_ID(N'{self.qualified}', N'U') IS NULL
            BEGIN
                CREATE TABLE {self.qualified} (
                    [id] BIGINT IDENTITY(1,1) PRIMARY KEY,
                    [revision_id] BIGINT NOT NULL,
                    [page_id] BIGINT NOT NULL,
                    [section_no] INT NOT NULL,
                    [paragraph_no] INT NOT NULL,
                    [toc] NVARCHAR(MAX) NULL,
                    [text] NVARCHAR(MAX) NULL,
                    CONSTRAINT {self._unique_constraint}
                        UNIQUE ([revision_id], [section_no], [paragraph_no])
                );
            END
            IF COL_LENGTH(N'{self.object_name}', N'page_title') IS NULL
                ALTER TABLE {self.qualified} ADD [page_title] NVARCHAR(512) NULL;
            IF COL_LENGTH(N'{self.object_name}', N'raw_wikitext') IS NULL
                ALTER TABLE {self.qualified} ADD [raw_wikitext] NVARCHAR(MAX) NULL;
            IF COL_LENGTH(N'{self.object_name}', N'parse_error') IS NULL
                ALTER TABLE {self.qualified} ADD [parse_error] NVARCHAR(MAX) NULL;
            {drop_legacy_columns_sql(self.qualified, self.object_name, ('processed_at',))}
            """)
        self._conn.commit()

    def add_rows(self, bundles: list[dict[str, Any]]) -> None:
        """接收一批数据并追加到内部缓冲区。"""
        for b in bundles:
            self._pending_revision_ids.add(b["revision_id"])
            for p in b["paragraphs"]:
                self._buffer.append(
                    (b["revision_id"], b["page_id"], b["page_title"],
                     p.section_no, p.paragraph_no, p.toc, p.raw_wikitext,
                     p.text, p.parse_error)
                )
        if (len(self._buffer) >= self.batch_size
                or len(self._pending_revision_ids) >= self.batch_size):
            self.flush()

    def flush(self) -> None:
        """将当前缓冲的数据写入目标位置。"""
        if not self._buffer and not self._pending_revision_ids:
            return
        try:
            # 在一个事务中先删后插；空结果也会清理已处理 revisions 的旧行。
            _delete_by_keys(
                self._conn, self.qualified, "revision_id", self._pending_revision_ids
            )
            if self._buffer:
                _insert_multirow(self._conn, self.qualified, self._COLS, self._buffer)
            self._conn.commit()
            self.written += len(self._buffer)
            self._buffer.clear()
            self._pending_revision_ids.clear()
        except Exception:
            self._conn.rollback()
            raise

    def close(self) -> None:
        """刷新未写入的数据并释放相关资源。"""
        self.flush()
        self._conn.close()


class WtpIntermediateWriter:
    """保存未跳过的 WTP 边界，并按 revision 先删后插。"""

    _COLS = (
        "revision_id", "page_id", "page_title", "section_no", "paragraph_no",
        "toc", "wtp_input", "expanded_wikitext", "parse_error",
    )

    def __init__(
        self,
        config: dict[str, Any],
        schema: str,
        table: str = "wiki_wtp_intermediate",
        batch_size: int = 500,
        db_timeout: float = 300.0,
        login_timeout: float = 60.0,
    ) -> None:
        """初始化对象所需的状态和资源。"""
        self.batch_size = batch_size
        self.qualified = qualified_name(schema, table)
        self.object_name = self.qualified
        self._unique_constraint = unique_constraint_name(table, "rev_secno_pno")
        self._buffer: list[tuple[Any, ...]] = []
        self._pending_revision_ids: set[Any] = set()
        self.written = 0
        self._conn = connect(
            config, autocommit=False, db_timeout=db_timeout, login_timeout=login_timeout
        )
        self._create_table()

    def _create_table(self) -> None:
        """执行创建数据表的处理逻辑。"""
        with self._conn.cursor() as cur:
            cur.execute(f"""
            IF OBJECT_ID(N'{self.qualified}', N'U') IS NULL
            BEGIN
                CREATE TABLE {self.qualified} (
                    [id] BIGINT IDENTITY(1,1) PRIMARY KEY,
                    [revision_id] BIGINT NOT NULL,
                    [page_id] BIGINT NOT NULL,
                    [page_title] NVARCHAR(512) NOT NULL,
                    [section_no] INT NOT NULL,
                    [paragraph_no] INT NOT NULL,
                    [toc] NVARCHAR(MAX) NULL,
                    [wtp_input] NVARCHAR(MAX) NULL,
                    [expanded_wikitext] NVARCHAR(MAX) NULL,
                    [parse_error] NVARCHAR(MAX) NULL,
                    CONSTRAINT {self._unique_constraint}
                        UNIQUE ([revision_id], [section_no], [paragraph_no])
                );
            END
            IF COL_LENGTH(N'{self.object_name}', N'page_title') IS NULL
                ALTER TABLE {self.qualified} ADD [page_title] NVARCHAR(512) NULL;
            IF COL_LENGTH(N'{self.object_name}', N'wtp_input') IS NULL
                ALTER TABLE {self.qualified} ADD [wtp_input] NVARCHAR(MAX) NULL;
            IF COL_LENGTH(N'{self.object_name}', N'expanded_wikitext') IS NULL
                ALTER TABLE {self.qualified} ADD [expanded_wikitext] NVARCHAR(MAX) NULL;
            IF COL_LENGTH(N'{self.object_name}', N'parse_error') IS NULL
                ALTER TABLE {self.qualified} ADD [parse_error] NVARCHAR(MAX) NULL;
            {drop_legacy_columns_sql(self.qualified, self.object_name, ('processed_at',))}
            """)
        self._conn.commit()

    def add_rows(self, bundles: list[dict[str, Any]]) -> None:
        """接收一批数据并追加到内部缓冲区。"""
        for b in bundles:
            self._pending_revision_ids.add(b["revision_id"])
            for p in b["paragraphs"]:
                if p.wtp_skipped:
                    continue
                self._buffer.append(
                    (b["revision_id"], b["page_id"], b["page_title"],
                     p.section_no, p.paragraph_no, p.toc, p.wtp_input,
                     p.expanded_wikitext, p.parse_error)
                )
        if len(self._buffer) >= self.batch_size:
            self.flush()

    def flush(self) -> None:
        """将当前缓冲的数据写入目标位置。"""
        if not self._buffer and not self._pending_revision_ids:
            return
        try:
            _delete_by_keys(
                self._conn, self.qualified, "revision_id", set(self._pending_revision_ids)
            )
            if self._buffer:
                _insert_multirow(self._conn, self.qualified, self._COLS, self._buffer)
            self._conn.commit()
            self.written += len(self._buffer)
            self._buffer.clear()
            self._pending_revision_ids.clear()
        except Exception:
            self._conn.rollback()
            raise

    def close(self) -> None:
        """刷新未写入的数据并释放相关资源。"""
        self.flush()
        self._conn.close()


class SentencesWriter:
    """sentence 写入器：每次 flush 先删后插，以清除陈旧记录。"""

    _COLS = ("revision_id", "page_id", "page_title", "section_no", "paragraph_no",
             "sentence_no", "toc", "raw_text", "text")

    def __init__(
        self,
        config: dict[str, Any],
        schema: str,
        table: str = "wiki_sentence",
        batch_size: int = 500,
        db_timeout: float = 300.0,
        login_timeout: float = 60.0,
    ) -> None:
        """初始化对象所需的状态和资源。"""
        self.batch_size = batch_size
        self.qualified = qualified_name(schema, table)
        self.object_name = self.qualified
        self._unique_constraint = unique_constraint_name(table, "rev_secno_pno_sno")
        self._buffer: list[tuple[Any, ...]] = []
        self._pending_revision_ids: set[Any] = set()
        self.written = 0
        self._conn = connect(
            config, autocommit=False, db_timeout=db_timeout, login_timeout=login_timeout
        )
        self._create_table()

    def _create_table(self) -> None:
        """执行创建数据表的处理逻辑。"""
        with self._conn.cursor() as cur:
            cur.execute(f"""
            IF OBJECT_ID(N'{self.qualified}', N'U') IS NULL
            BEGIN
                CREATE TABLE {self.qualified} (
                    [id] BIGINT IDENTITY(1,1) PRIMARY KEY,
                    [revision_id] BIGINT NOT NULL,
                    [page_id] BIGINT NOT NULL,
                    [page_title] NVARCHAR(512) NOT NULL,
                    [section_no] INT NOT NULL,
                    [paragraph_no] INT NOT NULL,
                    [sentence_no] INT NOT NULL,
                    [toc] NVARCHAR(MAX) NULL,
                    [raw_text] NVARCHAR(MAX) NULL,
                    [text] NVARCHAR(MAX) NULL,
                    CONSTRAINT {self._unique_constraint}
                        UNIQUE ([revision_id], [section_no], [paragraph_no], [sentence_no])
                );
            END
            IF COL_LENGTH(N'{self.object_name}', N'page_title') IS NULL
                ALTER TABLE {self.qualified} ADD [page_title] NVARCHAR(512) NULL;
            {drop_legacy_columns_sql(self.qualified, self.object_name, ('processed_at',))}
            """)
        self._conn.commit()

    def add_rows(self, bundles: list[dict[str, Any]]) -> None:
        """接收一批数据并追加到内部缓冲区。"""
        for b in bundles:
            self._pending_revision_ids.add(b["revision_id"])
            for s in b["sentences"]:
                self._buffer.append(
                    (b["revision_id"], b["page_id"], b["page_title"],
                     s.section_no, s.paragraph_no, s.sentence_no, s.toc,
                     s.raw_text, s.text)
                )
        if len(self._buffer) >= self.batch_size:
            self.flush()

    def flush(self) -> None:
        """将当前缓冲的数据写入目标位置。"""
        if not self._buffer and not self._pending_revision_ids:
            return
        try:
            _delete_by_keys(
                self._conn, self.qualified, "revision_id", set(self._pending_revision_ids)
            )
            if self._buffer:
                _insert_multirow(self._conn, self.qualified, self._COLS, self._buffer)
            self._conn.commit()
            self.written += len(self._buffer)
            self._buffer.clear()
            self._pending_revision_ids.clear()
        except Exception:
            self._conn.rollback()
            raise

    def close(self) -> None:
        """刷新未写入的数据并释放相关资源。"""
        self.flush()
        self._conn.close()


# --------------------------------------------------------------------------- #
# 将一条 bundle 数据流分发给多个写入器（分别调用其 add_rows / close）。
# --------------------------------------------------------------------------- #
class FanoutWriter:
    def __init__(self, *writers: Any) -> None:
        """初始化对象所需的状态和资源。"""
        self._writers = writers

    def add_rows(self, bundles: list[dict[str, Any]]) -> None:
        """接收一批数据并追加到内部缓冲区。"""
        for w in self._writers:
            w.add_rows(bundles)

    def flush(self) -> None:
        """将当前缓冲的数据写入目标位置。"""
        for w in self._writers:
            w.flush()

    def close(self) -> None:
        """刷新未写入的数据并释放相关资源。"""
        first_err: BaseException | None = None
        for w in self._writers:  # 即使某个写入器失败，也要关闭其余写入器
            try:
                w.close()
            except BaseException as exc:  # noqa: BLE001
                if first_err is None:
                    first_err = exc
        if first_err is not None:
            raise first_err


class CheckpointCoordinator:
    """仅当原始数据和处理结果都提交后，才将批次标记为完成。"""

    def __init__(self, store: Any) -> None:
        """初始化对象所需的状态和资源。"""
        self._store = store
        self._lock = threading.Lock()
        self._raw_committed: set[int] = set()
        self._processed_committed: dict[int, list[dict[str, Any]]] = {}

    def raw_committed(self, batch_id: int) -> None:
        """执行原始committed的处理逻辑。"""
        with self._lock:
            self._raw_committed.add(batch_id)
            self._mark_if_ready(batch_id)

    def processed_committed(
        self, batch_id: int, bundles: list[dict[str, Any]]
    ) -> None:
        """执行已处理committed的处理逻辑。"""
        with self._lock:
            self._processed_committed[batch_id] = bundles
            self._mark_if_ready(batch_id)

    def _mark_if_ready(self, batch_id: int) -> None:
        """执行markifready的处理逻辑。"""
        bundles = self._processed_committed.get(batch_id)
        if batch_id not in self._raw_committed or bundles is None:
            return
        self._store.mark_terminal(bundles)
        self._raw_committed.remove(batch_id)
        del self._processed_committed[batch_id]


# --------------------------------------------------------------------------- #
# 多阶段流水线编排，并独立监控各阶段速度。
#
#   读取线程 --futures_q--> 汇总线程 --write_q--> 写入线程
#        │（读取）                  │（解析并等待结果）          │（写入）
#    读取进度条                 解析进度条                  写入进度条
#
# 每个 tqdm 速度条只由一个线程更新，实时显示同步的读取、解析和写入速度。
# 两个有界队列负责背压：任何阶段变慢时，上游会自动阻塞，内存占用保持稳定。
# --------------------------------------------------------------------------- #
_SENTINEL = object()


def _run_core(
    batches: Iterable[list[dict[str, Any]]],
    processed_writer: Any,
    *,
    workers: int,
    total: int | None = None,
    raw_writer: Any | None = None,
    task_timeout: float | None = 300.0,
    max_count: int | None = None,
    key_column: str = KEY_COLUMN,
    source_close: Callable[[], None] = lambda: None,
    wtp_settings: Mapping[str, Any] | None = None,
    checkpoint: CheckpointCoordinator | None = None,
    failure_logger: Callable[[str], None] | None = None,
) -> tuple[int, int]:
    """执行运行核心的处理逻辑。"""
    if checkpoint is not None and raw_writer is None:
        raise ValueError("A checkpoint requires a raw writer acknowledgement.")
    # 各阶段分别显示速度条：读取 / 解析 / 写入（启用原始数据写入时还会显示 Raw）。
    bar_read = tqdm(
        total=total, desc="Read ", unit="row", position=0, file=sys.stdout
    )
    bar_parse = tqdm(
        total=total, desc="Parse", unit="row", position=1, file=sys.stdout
    )
    bar_write = tqdm(
        total=total, desc="Write", unit="row", position=2, file=sys.stdout
    )
    bar_raw = (
        tqdm(total=total, desc="Raw  ", unit="row", position=3, file=sys.stdout)
        if raw_writer
        else None
    )

    futures_q: "queue.Queue[Any]" = queue.Queue(maxsize=workers * 2)
    write_q: "queue.Queue[Any]" = queue.Queue(maxsize=workers * 2)
    raw_q: "queue.Queue[Any] | None" = queue.Queue(maxsize=workers * 2) if raw_writer else None
    # 多个线程可能同时出错；通过锁只保留最先记录的异常作为主要原因，
    # 确保顺序确定，不依赖 CPython 的 list.append 恰好具有原子性。
    error_lock = threading.Lock()
    first_error: list[BaseException] = []
    executor = ProcessPoolExecutor(
        max_workers=workers,
        initializer=initialize_wtp_worker,
        initargs=(wtp_settings,),
    )

    # 全局停止信号：任何线程出错都会设置此信号。所有 put/get 操作都会轮询它，
    # 因此下游线程先退出时，上游不会因队列已满而永久阻塞，join() 也不会一直等待。
    stop = threading.Event()
    # 强制中止信号：工作进程超过超时时间仍无响应时设置；清理阶段会强制结束进程，
    # 并且不等待其正常关闭。
    hard_abort = threading.Event()
    POLL = 0.2  # put/get/result 阻塞时的轮询间隔（秒），兼顾背压和停止响应速度

    # 累计失败数量，用于进度显示和最终汇总。
    # 单条失败记录只通过 failure_logger 写入运行日志。
    failed_total = 0
    paragraph_total = 0
    paragraph_succeeded = 0
    paragraph_failed = 0
    wtp_error_total = 0

    def fail(exc: BaseException) -> None:
        """执行失败的处理逻辑。"""
        with error_lock:
            if not first_error:  # 只记录第一个错误，忽略后续错误
                first_error.append(exc)
        stop.set()

    def safe_put(q: "queue.Queue[Any]", item: Any) -> bool:
        """执行安全put的处理逻辑。"""
        while not stop.is_set():
            try:
                q.put(item, timeout=POLL)
                return True
            except queue.Full:
                continue
        return False

    def safe_get(q: "queue.Queue[Any]") -> Any:
        """执行安全get的处理逻辑。"""
        while not stop.is_set():
            try:
                return q.get(timeout=POLL)
            except queue.Empty:
                continue
        return _SENTINEL

    def reader_loop() -> None:
        """执行readerloop的处理逻辑。"""
        submitted = 0
        next_batch_id = 0
        try:
            for batch in batches:
                if stop.is_set():
                    break
                if max_count is not None:  # 达到上限时截断跨越上限的批次，并在读取足量数据后停止
                    remaining = max_count - submitted
                    if remaining <= 0:
                        break
                    if len(batch) > remaining:
                        batch = batch[:remaining]
                batch_id = next_batch_id
                next_batch_id += 1
                expand_timeout = float(
                    wtp_settings.get("expand_timeout", 60.0)
                    if wtp_settings else 60.0
                )
                fut = executor.submit(process_batch, batch, expand_timeout)
                # 将此批次的键轻量地附加到 future，以便超时时定位问题数据。
                keys = [row[key_column] for row in batch]
                # 将同一批次分流：启用时写入原始数据表，同时发送到进程池解析。
                if raw_q is not None and not safe_put(raw_q, (batch_id, batch)):
                    break
                if not safe_put(futures_q, (fut, keys, batch_id)):
                    break
                bar_read.update(len(batch))
                submitted += len(batch)
                if max_count is not None and submitted >= max_count:
                    break
        except BaseException as exc:  # noqa: BLE001
            fail(exc)
        finally:
            safe_put(futures_q, _SENTINEL)  # 设置停止信号后不会阻塞
            if raw_q is not None:
                safe_put(raw_q, _SENTINEL)

    def wait_result(fut: Any) -> Any:
        """执行等待result的处理逻辑。"""
        start = time.monotonic()
        while not stop.is_set():
            try:
                return fut.result(timeout=POLL)
            except FuturesTimeout:
                if task_timeout and (time.monotonic() - start) >= task_timeout:
                    raise
        return _SENTINEL

    def collector_loop() -> None:
        """执行collectorloop的处理逻辑。"""
        nonlocal failed_total, paragraph_total, paragraph_succeeded
        nonlocal paragraph_failed, wtp_error_total
        try:
            while True:
                item = safe_get(futures_q)
                if item is _SENTINEL:
                    break
                fut, keys, batch_id = item
                try:
                    result = wait_result(fut)  # BatchResult 或 _SENTINEL（停止信号）
                except FuturesTimeout:
                    # 工作任务卡住或严重变慢：定位问题键，强制中止并尽快报错。
                    lo, hi = (min(keys), max(keys)) if keys else ("?", "?")
                    msg = (
                        f"worker parse timed out (> {task_timeout}s), "
                        f"{key_column} in [{lo}..{hi}]; workers force-reclaimed."
                        f" Inspect that key range, then re-run."
                    )
                    tqdm.write(f"[task-timeout] {msg}")
                    hard_abort.set()
                    fail(TimeoutError(msg))
                    break
                if result is _SENTINEL:  # 停止信号设置后被唤醒
                    break
                bar_parse.update(len(result.records))
                paragraph_total += result.paragraphs_total
                paragraph_succeeded += result.paragraphs_succeeded
                paragraph_failed += result.paragraphs_failed
                wtp_error_total += result.wtp_errors
                if result.failures:
                    failed_total += len(result.failures)
                    for key, err in result.failures:
                        line = f"[parse-fail] {key_column}={key}: {err}"
                        if failure_logger is not None:
                            failure_logger(line)
                if not safe_put(write_q, (batch_id, result.records)):
                    break
        except BaseException as exc:  # noqa: BLE001
            fail(exc)
        finally:
            safe_put(write_q, _SENTINEL)

    def writer_loop() -> None:
        """执行写入器loop的处理逻辑。"""
        try:
            while True:
                item = safe_get(write_q)
                if item is _SENTINEL:
                    break
                batch_id, records = item
                processed_writer.add_rows(records)
                if checkpoint is not None:
                    processed_writer.flush()
                    checkpoint.processed_committed(batch_id, records)
                bar_write.update(len(records))
        except BaseException as exc:  # noqa: BLE001
            fail(exc)

    def raw_writer_loop() -> None:
        """执行原始写入器loop的处理逻辑。"""
        try:
            while True:
                item = safe_get(raw_q)
                if item is _SENTINEL:
                    break
                batch_id, records = item
                raw_writer.add_rows(records)
                if checkpoint is not None:
                    raw_writer.flush()
                    checkpoint.raw_committed(batch_id)
                bar_raw.update(len(records))
        except BaseException as exc:  # noqa: BLE001
            fail(exc)

    threads = [
        threading.Thread(target=reader_loop, name="reader", daemon=True),
        threading.Thread(target=collector_loop, name="collector", daemon=True),
        threading.Thread(target=writer_loop, name="writer", daemon=True),
    ]
    if raw_writer is not None:
        threads.append(threading.Thread(target=raw_writer_loop, name="raw-writer", daemon=True))
    cleanup_errors: list[BaseException] = []

    def _safe(action: Callable[[], Any]) -> None:
        """执行安全的处理逻辑。"""
        try:
            action()
        except BaseException as exc:  # noqa: BLE001
            cleanup_errors.append(exc)

    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        # 清理步骤彼此隔离：某一步失败不会影响其他步骤。收集所有异常后统一判定，
        # 清理异常绝不能掩盖真正的根因（first_error 或 try 中传播出的异常）。
        if hard_abort.is_set():
            # 工作进程卡住：强制结束进程且不等待，否则 shutdown(wait=True) 也会一直挂起。
            _safe(lambda: terminate_workers(executor))
            _safe(lambda: executor.shutdown(wait=False, cancel_futures=True))
        else:
            # 正常结束或可恢复错误：cancel_futures 会取消排队任务，无需等待整个队列处理完。
            _safe(lambda: executor.shutdown(wait=True, cancel_futures=True))
        _safe(processed_writer.close)  # 包含最后一次 flush；若此处失败且没有其他根因，应将其作为真实错误传播
        if raw_writer is not None:
            _safe(raw_writer.close)
        _safe(source_close)
        for bar in (bar_write, bar_parse, bar_read):
            _safe(bar.close)
        if bar_raw is not None:
            _safe(bar_raw.close)

    # 判定主要原因：如果存在 first_error，则以它为准（清理异常降级为警告）；
    # 否则，清理异常本身就是主要原因，例如最后一次 flush 失败可能造成数据丢失，
    # 因此必须传播该异常。
    primary = first_error[0] if first_error else (cleanup_errors[0] if cleanup_errors else None)
    for exc in cleanup_errors:
        if exc is not primary:
            logging.warning(
                "Cleanup-stage exception (does not mask the primary cause): %s: %s",
                type(exc).__name__, exc,
            )
    if primary is not None:
        raise primary
    if paragraph_failed or wtp_error_total:
        logging.warning(
            "Paragraph stats: total=%s, succeeded=%s, failed=%s, wtp_lua_errors=%s",
            paragraph_total,
            paragraph_succeeded,
            paragraph_failed,
            wtp_error_total,
        )
    return int(bar_write.n), failed_total
