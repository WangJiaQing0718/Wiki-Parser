#!/usr/bin/env python3
"""
Processing engine: batches -> multiprocess parse -> write processed table
(optionally fan out to the raw table at the same time).

Called by wikipedia_parser.py. The core is `_run_core(batches, processed_writer,
raw_writer=...)`: decoupled from the batch source -- batches come from the XML
stream, get parsed in a process pool with mwparserfromhell (CPU-bound, bypassing
the GIL); read / parse / write (+ raw write) are each driven by one thread, each
with its own tqdm speed bar. Includes backpressure, worker-timeout fallback,
first-error semantics, cleanup that never masks the root cause, and observable
parse failures.

Reference implementation: for each ns=0 article run the chain
  1. extract_sections      (section_extractor: split by == Title ==)
  2. extract_and_templatize + remove_format_blocks per section
     (7 component types: infobox / table / wikilinks / external_links /
      file / image / ref; skip-toc sections kept verbatim)
  3. extract_paragraphs    (paragraph_extractor: blank-line split + subsection toc)
  4. extract_sentences     (sentence_extractor: sentencex + wikilink/list handling)
and write each type to its own table via ComponentWriter plus one row per
section / paragraph / sentence via SectionsWriter / ParagraphsWriter /
SentencesWriter. `_run_core` stays generic -- swap the extractor + sink to
change what gets written.

"""

from __future__ import annotations

import queue
import re
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

import mwparserfromhell
import pymssql
from mwparserfromhell.nodes import ExternalLink, Tag, Template, Wikilink
from tqdm import tqdm

# Hierarchical extraction (independent copies of API-Parser's lib/ modules,
# kept functionally aligned): sections -> paragraphs -> sentences.
from section_extractor import extract_sections  # noqa: E402
from paragraph_extractor import extract_paragraphs  # noqa: E402
from sentence_extractor import extract_sentences  # noqa: E402
from wtp_integration import (  # noqa: E402
    analyze_wikitext,
    begin_page,
    expand_to_text,
    initialize_wtp_worker,
)

# Primary key (both raw and processed records use revision_id; timeout locating
# and logging rely on it).
KEY_COLUMN = "revision_id"


@dataclass
class BatchResult:
    """Return value of process_batch: records to write + parse-failure details (for data-quality observability)."""

    records: list[dict[str, Any]]        # records to write (includes failed rows; failed rows have a non-null parse_error)
    failures: list[tuple[Any, str]]      # (source key value, error message), for counting and logging
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
# Parse-implementer definitions (below is a reference impl, replaceable wholesale).
#
# Each ns=0 article yields:
#   * text_process -- the wikitext with each component replaced in place by its id
#   * components    -- {type: [(component_id, component_text), ...]} in doc order
#   * sections / paragraphs / sentences -- the three hierarchical row lists
# Written by ProcessedTextWriter (text_process) + ComponentWriter (components)
# + SectionsWriter / ParagraphsWriter / SentencesWriter (hierarchical rows).
# =========================================================================== #
# Component types; each gets its own table component_<type>.
# file/image are split: [[File:...]] -> file table, [[Image:...]] -> image table.
COMPONENT_TYPES = (
    "infobox", "table", "wikilinks", "external_links", "file", "image", "ref",
)

# List blocks are deliberately protected from component extraction.  They stay
# untouched through WTP and are handled only by sentence_extractor.
_LIST_MARKER_RE = re.compile(r"^[ \t]*[*#;:]")

# Sections whose toc (lower-cased) is in this set are kept verbatim: no
# component extraction, no format-block removal, no sentence segmentation.
# (Independent copy of API-Parser's _SKIP_COMPONENT_TOCS.)
_SKIP_COMPONENT_TOCS = frozenset({
    "references",
    "other websites",
    "related pages",
    "more reading",
    "category",
    "external links",
    "footnotes",
    "notes"
})

# The current WTP pipeline still extracts components from these sections, but
# their paragraphs are stored as-is after that extraction and never expanded
# or split into sentences.
_SKIP_WTP_TOC_ROOTS = _SKIP_COMPONENT_TOCS


def should_skip_wtp_for_toc(toc: str | None) -> bool:
    """Return whether a paragraph toc belongs to a paragraph-only section."""
    if not toc:
        return False
    root = toc.split(":", 1)[0].strip().casefold()
    return root in _SKIP_WTP_TOC_ROOTS

# These are purely formatting HTML blocks that may appear in raw wikitext.
# They are removed after component extraction, before paragraph/sentence
# splitting. The regex allows attributes to span multiple lines and lets
# quoted attribute values contain `>`.
# (Independent copy of API-Parser's _FORMAT_TAG_RE.)
_FORMAT_TAG_RE = re.compile(
    r"<\s*(?P<closing>/\s*)?(?P<tag>templatestyles|div)\b"
    r"(?P<attrs>(?:\"[^\"]*\"|'[^']*'|[^>])*)>",
    re.IGNORECASE | re.DOTALL,
)

# Placeholders emitted by extract_and_templatize:
# ♣  ♣  ♣  <type>-<page_id>-<seq>♣  ♣  ♣
_PLACEHOLDER_RE = re.compile(
    r"♣  ♣  ♣  ((?:" + "|".join(COMPONENT_TYPES) + r")-[^♣\r\n]+?)♣  ♣  ♣"
)


def remove_format_blocks(wikitext: str | None) -> str:
    """从 wikitext 中移除 ``templatestyles`` 和 ``div`` 格式块。

    会删除完整成对的块（含内部内容）以及自闭合形式。其他 HTML 标签和普通文本
    都保持不变。这里采用一个小型栈式扫描过程，能够处理嵌套格式块，
    同时尽量避免留下孤立的闭合标签。
    """
    if not wikitext:
        return ""

    open_blocks: list[tuple[str, int]] = []
    remove_spans: list[tuple[int, int]] = []

    for match in _FORMAT_TAG_RE.finditer(wikitext):
        tag = match.group("tag").lower()
        if match.group("closing"):
            # 对于结构正常的 HTML，闭合标签应匹配最近打开的目标标签。
            # 倒序查找可以容忍不规范嵌套，同时避免误删无关文本。
            matching_index = next(
                (i for i in range(len(open_blocks) - 1, -1, -1)
                 if open_blocks[i][0] == tag),
                None,
            )
            if matching_index is None:
                continue

            start = open_blocks[matching_index][1]
            # 闭合标签会结束对应块以及其内部嵌套的目标块。
            # 下方会合并重叠区间，因此即便外层块不规范或未闭合，
            # 也能尽量完整移除已闭合的内层块。
            del open_blocks[matching_index:]
            remove_spans.append((start, match.end()))
            continue

        attrs = match.group("attrs")
        if attrs.rstrip().endswith("/"):
            remove_spans.append((match.start(), match.end()))
        else:
            open_blocks.append((tag, match.start()))

    # 未闭合的起始标签保持原样，仅移除完整闭合的块。
    # 先合并重叠区间（例如成对块中的自闭合标签），
    # 这样删除前一个区间时不会破坏后一个区间的偏移量。
    merged_spans: list[tuple[int, int]] = []
    for start, end in sorted(remove_spans):
        if merged_spans and start <= merged_spans[-1][1]:
            previous_start, previous_end = merged_spans[-1]
            merged_spans[-1] = (previous_start, max(previous_end, end))
        else:
            merged_spans.append((start, end))

    for start, end in reversed(merged_spans):
        wikitext = wikitext[:start] + wikitext[end:]
    return wikitext


def _component_id(ctype: str, page_id: Any, seq: int) -> str:
    """Per-page, per-type id embedded in text_process and stored as <type>_id."""
    return f"{ctype}-{page_id}-{seq:04d}"


def _protect_list_blocks(wikitext: str) -> tuple[str, dict[str, str]]:
    """Temporarily replace contiguous wiki-list blocks with inert markers.

    This is not component extraction: the raw blocks are restored before the
    text is passed to WTP.  It simply prevents MWP's inline component pass from
    extracting links, refs, files, etc. inside a list that sentence processing
    must receive as an intact hierarchy.
    """
    lines = wikitext.splitlines(keepends=True)
    output: list[str] = []
    blocks: dict[str, str] = {}
    index = 0
    position = 0
    while position < len(lines):
        if not _LIST_MARKER_RE.match(lines[position]):
            output.append(lines[position])
            position += 1
            continue

        start = position
        while position < len(lines) and _LIST_MARKER_RE.match(lines[position]):
            position += 1
        token = f"@@WIKILISTBLOCKTOKEN{index}@@"
        while token in wikitext:
            index += 1
            token = f"@@WIKILISTBLOCKTOKEN{index}@@"
        blocks[token] = "".join(lines[start:position])
        output.append(token)
        index += 1
    return "".join(output), blocks


def extract_and_templatize(
    wikitext: str | None, page_id: Any,
    comps: dict[str, list[tuple[str, str]]] | None = None,
) -> tuple[str, dict[str, list[tuple[str, str]]]]:
    """Extract components AND build the templatized text for one piece of wikitext
    (a whole article, or one section when called per-section from build_bundle).

    comps: optional pre-allocated accumulator for the extracted components. When
        build_bundle calls this once per section, passing the SAME dict keeps the
        per-type <seq> in the component id page-global (0001 = first of that type
        on the page) instead of restarting per section. A fresh dict is created
        when omitted.

    Returns (text_process, components):
      * text_process -- the wikitext with each extracted component replaced in place
        by ``♣  ♣  ♣  <type>-<page_id>-<seq>♣  ♣  ♣``, EXCEPT
        wikilinks, which are extracted to their table but left as raw ``[[...]]`` in
        text_process; all other markup is kept verbatim. Only the placeholders from
        component extraction are produced -- no extra newline or whitespace adjustment.
      * components    -- {type: [(component_id, component_text), ...]} in document
        order (seq 0001 = first of that type on the page), so the id embedded in
        text_process matches the component row exactly.

    Component types: infobox / table (blocks) and wikilinks / external_links /
    file ([[File:...]]) / image ([[Image:...]]) / ref (<ref>...</ref>) (inline).

    Strict containment: a component nested inside a block (infobox / table) is
    NOT extracted separately -- it stays as raw text inside that block. Likewise a
    <ref> or File:/Image: link subsumes anything inside it (it is one node).

    Done in two phases over the text (no per-node tree mutation -> fast):
      1. block nodes (infobox templates, tables) -> replaced by their placeholder;
      2. remaining inline nodes via a re-parse (the club-delimited placeholders
         remain ordinary text -> untouched): <ref> -> ref, [[File:...]] -> file,
         [[Image:...]] -> image, other [[...]] -> wikilinks (kept raw),
         [url] -> external_links.

    Wiki list blocks are deliberately *not* components.  They and their nested
    markup remain in the WTP input so the sentence-stage list protection and
    hierarchical rendering can operate on WTP output.
    """
    if comps is None:
        comps = {t: [] for t in COMPONENT_TYPES}

    def take(ctype: str, text: str) -> str:
        cid = _component_id(ctype, page_id, len(comps[ctype]) + 1)
        comps[ctype].append((cid, text))
        return f"♣  ♣  ♣  {cid}♣  ♣  ♣"

    # Phase 1: block containers (infobox templates, tables). Top-level node scan;
    # inner content is subsumed into the block text.
    code = mwparserfromhell.parse(wikitext or "")
    parts: list[str] = []
    for node in code.nodes:
        if isinstance(node, Template) and str(node.name).strip().lower().startswith("infobox"):
            parts.append(take("infobox", str(node)))
        elif isinstance(node, Tag) and str(node.tag).strip().lower() == "table":
            parts.append(take("table", str(node)))
        else:
            parts.append(str(node))

    # Phase 2: remaining inline components.  Lists become inert temporary
    # markers only while this pass runs, and are restored before returning.
    # Re-parse; the club-delimited placeholders remain ordinary text. A <ref> or
    # File:/Image: link subsumes anything inside it (it is a single top-level node).
    #   * <ref>...</ref>             -> ref table, replaced by a placeholder
    #   * [[File:...]]              -> file table, replaced by a placeholder
    #   * [[Image:...]]             -> image table, replaced by a placeholder
    #   * other [[...]]              -> wikilinks table, kept raw in text_process
    #   * [url ...] / bare url       -> external_links table, replaced by a placeholder
    inline_source, list_blocks = _protect_list_blocks("".join(parts))
    code2 = mwparserfromhell.parse(inline_source)
    parts2: list[str] = []
    for node in code2.nodes:
        if isinstance(node, Tag) and str(node.tag).strip().lower() == "ref":
            parts2.append(take("ref", str(node)))
        elif isinstance(node, Wikilink):
            title = str(node.title).strip()
            if title.lower().startswith("image:"):
                parts2.append(take("image", str(node)))
            elif title.lower().startswith("file:"):
                parts2.append(take("file", str(node)))
            else:
                cid = _component_id("wikilinks", page_id, len(comps["wikilinks"]) + 1)
                comps["wikilinks"].append((cid, title))
                parts2.append(str(node))  # keep the raw [[...]] in text_process
        elif isinstance(node, ExternalLink):
            parts2.append(take("external_links", str(node.url).strip()))
        else:
            parts2.append(str(node))

    # 仅保留组件提取产生的占位符，不再额外调整换行或首尾空白。
    text = "".join(parts2)
    for token, raw_list in list_blocks.items():
        text = text.replace(token, raw_list)

    return text, comps


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


def _legacy_build_bundle(row: Mapping[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Parse one source row into (bundle, error).

    Chain (mirrors API-Parser's extract_worker; sentencex is imported lazily
    inside extract_sentences so this module stays importable without it):
      1. sections   -- extract_sections (split by == Title ==)
      2. per section -- extract_and_templatize (shared accumulator keeps the
         component id's <seq> page-global) + remove_format_blocks; placeholders
         swallowed by a removed format block are dropped from the components so
         rows always match text_process; sections whose toc is in
         _SKIP_COMPONENT_TOCS are kept verbatim. Each heading section's original
         ``== Title ==`` line is re-emitted into text_process so section
         boundaries stay visible there (Summary has no heading).
      3. paragraphs -- extract_paragraphs (blank-line split + subsection toc)
      4. sentences  -- extract_sentences (sentencex; skip-toc kept whole)

    bundle carries the processed-table fields (revision_id/page_id/page_title/
    namespace/text_process/parse_error) plus components = {type: [(id, text), ...]}
    and the three hierarchical row lists: sections / paragraphs / sentences
    (plain dataclasses of int/str fields -- safe to pickle across processes).
    error is None on success; on failure the bundle has empty components, empty
    hierarchical lists and text_process="". Non-wikitext pages are kept verbatim
    with no components and empty hierarchical lists.
    """
    try:
        if row.get("model") in (None, "wikitext"):
            # 1. 分节。
            sections = extract_sections(row.get("content"), row["page_id"])

            # 2. 逐节提取组件 + 删除格式化块；特殊节保持原样。
            # remove_format_blocks 会整段删除 <div>/<templatestyles>，连同其中
            # 已被替换成占位符的组件（阶段 2 的列表块按行提取、不感知标签嵌套，
            # 会提取到 div 内部）。占位符已不在正文里，对应组件行必须同步丢弃，
            # 否则成为无引用的孤儿行。
            # 丢弃集合在整页所有 section 处理完后一次性生效：组件 <seq> 取自
            # 提取时该类型列表的当前长度（len+1），若中途收缩，后续 section 会
            # 复用仍在使用的序号，产生重复组件 id（触发 UNIQUE 约束 2627）。
            components = {t: [] for t in COMPONENT_TYPES}
            dropped_ids: set[str] = set()
            all_text_parts: list[str] = []
            skip_tocs: set[str] = set()
            for section in sections:
                toc_key = section.toc.strip().lower() if section.toc else ""
                if toc_key in _SKIP_COMPONENT_TOCS:
                    all_text_parts.append(_section_part(section))
                    skip_tocs.add(toc_key)
                    continue
                sec_tp, _sec_comps = extract_and_templatize(
                    section.text, row["page_id"], components
                )
                sec_tp_after = remove_format_blocks(sec_tp)
                dropped_ids.update(
                    set(_PLACEHOLDER_RE.findall(sec_tp))
                    - set(_PLACEHOLDER_RE.findall(sec_tp_after))
                )
                section.text = sec_tp_after
                all_text_parts.append(_section_part(section))
            if dropped_ids:
                for t in components:
                    components[t] = [item for item in components[t]
                                     if item[0] not in dropped_ids]
            text_process = "\n\n".join(all_text_parts)

            # 3./4. 分段 → 分句。
            paragraphs = extract_paragraphs(sections, row["page_id"])
            sentences = extract_sentences(
                paragraphs, row["page_id"], skip_tocs=skip_tocs
            )
            error: str | None = None
        else:
            text_process = str(row.get("content") or "")
            sections = []
            paragraphs = []
            sentences = []
            components = {t: [] for t in COMPONENT_TYPES}
            error = None
    except Exception as exc:  # observable parse failure
        text_process = ""
        sections = []
        paragraphs = []
        sentences = []
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
    return bundle, error


def build_bundle(
    row: Mapping[str, Any], expand_timeout: float = 15.0
) -> tuple[dict[str, Any], str | None, ParagraphRunStats]:
    """Build an article bundle with MWP component placeholders before WTP."""
    stats = ParagraphRunStats()
    try:
        if row.get("model") in (None, "wikitext"):
            sections = extract_sections(row.get("content"), row["page_id"])
            raw_paragraphs = {
                (paragraph.section_no, paragraph.paragraph_no): paragraph.text
                for paragraph in extract_paragraphs(sections, row["page_id"])
            }
            components = {t: [] for t in COMPONENT_TYPES}
            # MWP-derived component placeholders are the WTP input.  There is
            # intentionally no per-template filtering, no skip-section branch,
            # and no format-block deletion.
            for section in sections:
                transformed, _ = extract_and_templatize(
                    section.text, row["page_id"], components
                )
                section.text = transformed
            text_process = "\n\n".join(_section_part(section) for section in sections)
            paragraphs = extract_paragraphs(sections, row["page_id"])

            # When a reference/link-like TOC root appears, that paragraph and
            # every following paragraph in the article stay paragraph-only.
            # Their toc may include a subsection suffix such as
            # ``References:Books``.
            wtp_paragraphs = []
            skip_remaining = False
            for paragraph in paragraphs:
                transformed_wikitext = paragraph.text
                paragraph.raw_wikitext = raw_paragraphs.get(
                    (paragraph.section_no, paragraph.paragraph_no),
                    transformed_wikitext,
                )
                paragraph.wtp_skipped = (
                    skip_remaining or should_skip_wtp_for_toc(paragraph.toc)
                )
                if paragraph.wtp_skipped:
                    skip_remaining = True
                    continue
                wtp_paragraphs.append(paragraph)

            # One page context, then isolated MWP/WTP handling for each
            # paragraph that was not skipped above.
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
    """Process a whole batch of source rows in a worker process; return a picklable BatchResult of bundles."""
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
# Generic engine (usually no changes needed): config, connection, orchestration.
# =========================================================================== #
def quote_ident(name: str) -> str:
    if not name:
        raise ValueError("Identifier must be non-empty.")
    return "[" + name.replace("]", "]]") + "]"


def qualified_name(schema: str, table: str) -> str:
    return f"{quote_ident(schema)}.{quote_ident(table)}"


def drop_legacy_columns_sql(
    qualified: str, object_name: str, columns: Sequence[str]
) -> str:
    """Drop named columns and any SQL Server default constraints they own."""
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
    """Forcibly reclaim the pool's workers (a wedged task cannot be joined gracefully), best-effort.

    The stdlib ProcessPoolExecutor cannot cancel an already-running task; only
    killing the process reclaims a worker held by a wedged task, and avoids
    shutdown(wait=True) hanging along with it.
    """
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
    # timeout = query timeout (seconds), prevents cur.execute()/executemany() from
    # blocking forever on network jitter or a stalled server (task-timeout only
    # covers parsing, not DB reads/writes); 0 = unlimited.
    # login_timeout = connect/login timeout (seconds), prevents connect itself from hanging.
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
# Processed-text writer: processed, one row per article, MERGE upsert
# on revision_id (idempotent). text_process = wikitext with components replaced by
# their ids. Upserts run as chunked multi-row MERGE statements -- one round trip
# per chunk instead of one per row.
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
        for b in bundles:
            self._buffer.append(tuple(b[c] for c in self._COLS))
        if len(self._buffer) >= self.batch_size:
            self.flush()

    def _build_merge_sql(self) -> str:
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
        if not self._buffer:
            return
        # Chunked multi-row MERGE: one round trip per chunk instead of one per
        # row; the chunk size keeps each statement below SQL Server's
        # 2100-parameter limit.
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
        self.flush()
        self._conn.close()


# --------------------------------------------------------------------------- #
# Component writer: one table per component type (component_<type>).
# Rows come pre-labelled with their <type>_id (assigned per-page in the worker so
# they match the ids embedded in text_process) plus the owning page_id.
# Delete-then-insert per flush, keyed on page_id (like the hierarchical writers
# keyed on revision_id): every row of every page seen since the last flush is
# deleted first -- including pages whose parse produced no components of that
# type -- so rows from an older run, or a changed parse chain that now emits
# fewer (or zero) components, are cleaned up automatically (stale-row safe).
#
# Each table carries a UNIQUE constraint on <type>_id (a duplicate id fails
# loudly at insert instead of accumulating silent dup rows) and a page_id index
# for the flush DELETE to seek; both live in the CREATE TABLE since tables are
# created fresh at startup.
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
        self.batch_size = batch_size
        self._table_names = {
            t: f"{table_prefix}_{t}{table_version_suffix}" for t in COMPONENT_TYPES
        }
        self._tables = {t: qualified_name(schema, name) for t, name in self._table_names.items()}
        self._object_names = dict(self._tables)
        self._buffers: dict[str, list[tuple[int, str, str]]] = {t: [] for t in COMPONENT_TYPES}
        # Pages seen since the last flush: the per-type DELETE keys. Every page
        # of the batch is included, even when its parse produced no rows for a
        # type -- otherwise stale rows of pages whose components vanished
        # entirely would never be deleted.
        self._pending_pages: set[int] = set()
        self.written: dict[str, int] = {t: 0 for t in COMPONENT_TYPES}
        self._conn = connect(
            config, autocommit=False, db_timeout=db_timeout, login_timeout=login_timeout
        )
        self._create_tables()

    def _create_tables(self) -> None:
        with self._conn.cursor() as cur:
            for t, q in self._tables.items():
                # Constraint/index names are database-scoped, hence the table name inside.
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
        for b in bundles:
            self._pending_pages.add(b["page_id"])
            for t, items in b["components"].items():
                # items are (component_id, text); prefix the owning page_id,
                # taken from the bundle (the worker side stays unchanged).
                self._buffers[t].extend((b["page_id"], cid, text) for cid, text in items)
        if any(len(buf) >= self.batch_size for buf in self._buffers.values()):
            self.flush()

    def flush(self) -> None:
        if not any(self._buffers.values()) and not self._pending_pages:
            return
        try:
            for t, buf in self._buffers.items():
                # Delete-then-insert, one transaction: the delete covers ALL
                # rows of every page seen since the last flush (even pages
                # whose parse produced no rows for this type), so a page's
                # component set is fully replaced (stale-row safe).
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
            # Counters and buffer clearing only after a successful commit: on
            # failure the rollback undoes every type's writes, so the buffers
            # must survive for the close() retry and `written` must not count
            # uncommitted rows.
            for t, buf in self._buffers.items():
                if buf:
                    self.written[t] += len(buf)
                    buf.clear()
            self._pending_pages.clear()
        except Exception:
            self._conn.rollback()
            raise

    def close(self) -> None:
        self.flush()
        self._conn.close()


# --------------------------------------------------------------------------- #
# Hierarchical writers: sections / paragraphs / sentences, one row per
# section / paragraph / sentence. Delete-then-insert per flush (like
# API-Parser): the rows of the revisions in the batch are deleted and
# re-inserted, so stale detail rows from previous runs (or a changed parse
# chain) are cleaned up automatically. Each revision_id appears in exactly
# one flush per run, so the delete never removes rows that are not re-inserted
# in the same transaction.
# --------------------------------------------------------------------------- #
def _delete_by_keys(
    conn: Any,
    qualified: str,
    column: str,
    keys: Iterable[Any],
    chunk_size: int = 500,
) -> None:
    """DELETE all rows whose key column is in `keys`, in chunks of at most
    chunk_size parameters (one statement per chunk)."""
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


def _delete_by_revision_ids(
    conn: Any, qualified: str, buffer: list[tuple[Any, ...]], chunk_size: int = 500
) -> None:
    """DELETE all rows of the revisions present in the buffer (revision_id is
    the first buffer column), in chunks of at most chunk_size parameters."""
    _delete_by_keys(conn, qualified, "revision_id", {row[0] for row in buffer}, chunk_size)


def _insert_multirow(
    conn: Any,
    qualified: str,
    columns: Sequence[str],
    rows: list[tuple[Any, ...]],
) -> None:
    """INSERT rows via multi-row VALUES statements: one round trip per chunk
    instead of one per row (executemany sends every row separately). The chunk
    size keeps each statement below SQL Server's 2100-parameter limit. The
    caller owns the transaction (commit / rollback)."""
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
    """sections: delete-then-insert per flush (stale-row safe)."""

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
        self.batch_size = batch_size
        self.qualified = qualified_name(schema, table)
        self.object_name = self.qualified
        self._buffer: list[tuple[Any, ...]] = []
        self.written = 0
        self._conn = connect(
            config, autocommit=False, db_timeout=db_timeout, login_timeout=login_timeout
        )
        self._create_table()

    def _create_table(self) -> None:
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
                    CONSTRAINT [UQ_sections_rev_secno] UNIQUE ([revision_id], [section_no])
                );
            END
            IF COL_LENGTH(N'{self.object_name}', N'page_title') IS NULL
                ALTER TABLE {self.qualified} ADD [page_title] NVARCHAR(512) NULL;
            {drop_legacy_columns_sql(self.qualified, self.object_name, ('processed_at',))}
            """)
        self._conn.commit()

    def add_rows(self, bundles: list[dict[str, Any]]) -> None:
        for b in bundles:
            for s in b["sections"]:
                self._buffer.append(
                    (b["revision_id"], b["page_id"], b["page_title"],
                     s.section_no, s.toc, s.raw_text, s.text)
                )
        if len(self._buffer) >= self.batch_size:
            self.flush()

    def flush(self) -> None:
        if not self._buffer:
            return
        try:
            # Delete-then-insert, one transaction: the delete targets exactly
            # the revisions being re-inserted below.
            _delete_by_revision_ids(self._conn, self.qualified, self._buffer)
            _insert_multirow(self._conn, self.qualified, self._COLS, self._buffer)
            self._conn.commit()
            self.written += len(self._buffer)
            self._buffer.clear()
        except Exception:
            self._conn.rollback()
            raise

    def close(self) -> None:
        self.flush()
        self._conn.close()


class ParagraphsWriter:
    """paragraph: delete-then-insert per flush (stale-row safe)."""

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
        self.batch_size = batch_size
        self.qualified = qualified_name(schema, table)
        self.object_name = self.qualified
        self._buffer: list[tuple[Any, ...]] = []
        self.written = 0
        self._conn = connect(
            config, autocommit=False, db_timeout=db_timeout, login_timeout=login_timeout
        )
        self._create_table()

    def _create_table(self) -> None:
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
                    CONSTRAINT [UQ_paragraph_rev_secno_pno]
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
        for b in bundles:
            for p in b["paragraphs"]:
                self._buffer.append(
                    (b["revision_id"], b["page_id"], b["page_title"],
                     p.section_no, p.paragraph_no, p.toc, p.raw_wikitext,
                     p.text, p.parse_error)
                )
        if len(self._buffer) >= self.batch_size:
            self.flush()

    def flush(self) -> None:
        if not self._buffer:
            return
        try:
            # Delete-then-insert, one transaction: the delete targets exactly
            # the revisions being re-inserted below.
            _delete_by_revision_ids(self._conn, self.qualified, self._buffer)
            _insert_multirow(self._conn, self.qualified, self._COLS, self._buffer)
            self._conn.commit()
            self.written += len(self._buffer)
            self._buffer.clear()
        except Exception:
            self._conn.rollback()
            raise

    def close(self) -> None:
        self.flush()
        self._conn.close()


class WtpIntermediateWriter:
    """Persist non-skipped WTP boundaries, delete-then-insert per revision."""

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
        self.batch_size = batch_size
        self.qualified = qualified_name(schema, table)
        self.object_name = self.qualified
        self._buffer: list[tuple[Any, ...]] = []
        self._pending_revision_ids: set[Any] = set()
        self.written = 0
        self._conn = connect(
            config, autocommit=False, db_timeout=db_timeout, login_timeout=login_timeout
        )
        self._create_table()

    def _create_table(self) -> None:
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
                    CONSTRAINT [UQ_wtp_intermediate_rev_secno_pno]
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
        self.flush()
        self._conn.close()


class SentencesWriter:
    """sentence: delete-then-insert per flush (stale-row safe)."""

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
        self.batch_size = batch_size
        self.qualified = qualified_name(schema, table)
        self.object_name = self.qualified
        self._buffer: list[tuple[Any, ...]] = []
        self._pending_revision_ids: set[Any] = set()
        self.written = 0
        self._conn = connect(
            config, autocommit=False, db_timeout=db_timeout, login_timeout=login_timeout
        )
        self._create_table()

    def _create_table(self) -> None:
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
                    CONSTRAINT [UQ_sentence_rev_secno_pno_sno]
                        UNIQUE ([revision_id], [section_no], [paragraph_no], [sentence_no])
                );
            END
            IF COL_LENGTH(N'{self.object_name}', N'page_title') IS NULL
                ALTER TABLE {self.qualified} ADD [page_title] NVARCHAR(512) NULL;
            {drop_legacy_columns_sql(self.qualified, self.object_name, ('processed_at',))}
            """)
        self._conn.commit()

    def add_rows(self, bundles: list[dict[str, Any]]) -> None:
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
        self.flush()
        self._conn.close()


# --------------------------------------------------------------------------- #
# Fan one stream of bundles out to several sink writers (each add_rows / close).
# --------------------------------------------------------------------------- #
class FanoutWriter:
    def __init__(self, *writers: Any) -> None:
        self._writers = writers

    def add_rows(self, bundles: list[dict[str, Any]]) -> None:
        for w in self._writers:
            w.add_rows(bundles)

    def flush(self) -> None:
        for w in self._writers:
            w.flush()

    def close(self) -> None:
        first_err: BaseException | None = None
        for w in self._writers:  # close every sink even if one fails
            try:
                w.close()
            except BaseException as exc:  # noqa: BLE001
                if first_err is None:
                    first_err = exc
        if first_err is not None:
            raise first_err


class CheckpointCoordinator:
    """Mark a batch terminal only after raw and processed outputs both commit."""

    def __init__(self, store: Any) -> None:
        self._store = store
        self._lock = threading.Lock()
        self._raw_committed: set[int] = set()
        self._processed_committed: dict[int, list[dict[str, Any]]] = {}

    def raw_committed(self, batch_id: int) -> None:
        with self._lock:
            self._raw_committed.add(batch_id)
            self._mark_if_ready(batch_id)

    def processed_committed(
        self, batch_id: int, bundles: list[dict[str, Any]]
    ) -> None:
        with self._lock:
            self._processed_committed[batch_id] = bundles
            self._mark_if_ready(batch_id)

    def _mark_if_ready(self, batch_id: int) -> None:
        bundles = self._processed_committed.get(batch_id)
        if batch_id not in self._raw_committed or bundles is None:
            return
        self._store.mark_terminal(bundles)
        self._raw_committed.remove(batch_id)
        del self._processed_committed[batch_id]


# --------------------------------------------------------------------------- #
# Multi-stage pipeline orchestration + per-stage independent speed monitoring
#
#   Reader thread --futures_q--> Collector thread --write_q--> Writer thread
#        │(read)                    │(parse, await result)       │(write)
#    Read bar                   Parse bar                    Write bar
#
# Each of the tqdm speed bars is updated by exactly one thread, showing the live
# read/parse/write rates in sync. Two bounded queues provide backpressure: when
# any stage slows down, upstream blocks automatically and memory stays constant.
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
    """Generic pipeline core: batch source -> multiprocess parse -> write processed table, optionally writing the raw table too (fan-out).

    batches: an iterable that yields "batches (list of row-dicts)" (in this
      project it comes from XmlBatchSource). Each row-dict has at least the keys
      process_batch needs (content / primary key / ...); if raw_writer is given,
      the same batch is also written verbatim to the raw table.
    processed_writer: processed-result writer (add_rows / close); e.g. ComponentWriter.
    raw_writer: optional. Raw-record writer (add_rows / close), a second output path parallel to parsing.
    total: progress-bar total; pass None for a streaming source of unknown size (shows count + rate only).
    source_close: close the batch source (file stream, etc.).
    checkpoint: optional two-writer commit barrier for XML resume state.
    Returns (rows written to processed table, parse-failure rows).
    """
    if checkpoint is not None and raw_writer is None:
        raise ValueError("A checkpoint requires a raw writer acknowledgement.")
    # Independent speed bars: Read / Parse / Write (+ Raw when raw writing is enabled).
    bar_read = tqdm(total=total, desc="Read ", unit="row", position=0)
    bar_parse = tqdm(total=total, desc="Parse", unit="row", position=1)
    bar_write = tqdm(total=total, desc="Write", unit="row", position=2)
    bar_raw = tqdm(total=total, desc="Raw  ", unit="row", position=3) if raw_writer else None

    futures_q: "queue.Queue[Any]" = queue.Queue(maxsize=workers * 2)
    write_q: "queue.Queue[Any]" = queue.Queue(maxsize=workers * 2)
    raw_q: "queue.Queue[Any] | None" = queue.Queue(maxsize=workers * 2) if raw_writer else None
    # First-error semantics: multiple threads may error at once; a lock guarantees
    # only the "first" exception is kept as the primary cause -- deterministic
    # order, not relying on CPython's list.append happening to be atomic.
    error_lock = threading.Lock()
    first_error: list[BaseException] = []
    executor = ProcessPoolExecutor(
        max_workers=workers,
        initializer=initialize_wtp_worker,
        initargs=(wtp_settings,),
    )

    # Global stop signal: any thread that errors sets it. All put/get poll it, so
    # when a downstream thread exits first, upstream will not block forever on a
    # full queue, and join() will not wait forever either.
    stop = threading.Event()
    # Hard abort: set when a worker wedges past the timeout; teardown force-kills
    # the processes + does not wait on shutdown.
    hard_abort = threading.Event()
    POLL = 0.2  # seconds: poll interval while put/get/result blocks (balances backpressure and stop responsiveness)

    # Data quality: accumulate the parse-failure count and print the first few in detail (avoid flooding).
    failed_total = 0
    paragraph_total = 0
    paragraph_succeeded = 0
    paragraph_failed = 0
    wtp_error_total = 0
    logged = 0
    LOG_LIMIT = 20

    def fail(exc: BaseException) -> None:
        with error_lock:
            if not first_error:  # record only the first error, ignore later ones
                first_error.append(exc)
        stop.set()

    def safe_put(q: "queue.Queue[Any]", item: Any) -> bool:
        """Put with backpressure; give up immediately and return False once stop is set."""
        while not stop.is_set():
            try:
                q.put(item, timeout=POLL)
                return True
            except queue.Full:
                continue
        return False

    def safe_get(q: "queue.Queue[Any]") -> Any:
        """Get; return _SENTINEL once stop is set so the consumer wraps up and exits."""
        while not stop.is_set():
            try:
                return q.get(timeout=POLL)
            except queue.Empty:
                continue
        return _SENTINEL

    def reader_loop() -> None:
        submitted = 0
        next_batch_id = 0
        try:
            for batch in batches:
                if stop.is_set():
                    break
                if max_count is not None:  # limit: truncate the batch that crosses the cap, stop once enough is read
                    remaining = max_count - submitted
                    if remaining <= 0:
                        break
                    if len(batch) > remaining:
                        batch = batch[:remaining]
                batch_id = next_batch_id
                next_batch_id += 1
                expand_timeout = float(
                    wtp_settings.get("expand_timeout", 15.0)
                    if wtp_settings else 15.0
                )
                fut = executor.submit(process_batch, batch, expand_timeout)
                # Attach this batch's keys to the future (lightweight), to locate problem data on timeout.
                keys = [row[key_column] for row in batch]
                # Fan-out: the same batch is both written to the raw table (if enabled) and sent to the pool to parse.
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
            safe_put(futures_q, _SENTINEL)  # does not block when stop is set
            if raw_q is not None:
                safe_put(raw_q, _SENTINEL)

    def wait_result(fut: Any) -> Any:
        """Await a single batch's result: poll stop and apply task_timeout to the whole batch.
        Returns _SENTINEL while stop is set; raises FuturesTimeout past task_timeout."""
        start = time.monotonic()
        while not stop.is_set():
            try:
                return fut.result(timeout=POLL)
            except FuturesTimeout:
                if task_timeout and (time.monotonic() - start) >= task_timeout:
                    raise
        return _SENTINEL

    def collector_loop() -> None:
        nonlocal failed_total, logged, paragraph_total, paragraph_succeeded
        nonlocal paragraph_failed, wtp_error_total
        try:
            while True:
                item = safe_get(futures_q)
                if item is _SENTINEL:
                    break
                fut, keys, batch_id = item
                try:
                    result = wait_result(fut)  # BatchResult / _SENTINEL (stop)
                except FuturesTimeout:
                    # A worker task is wedged/very slow: locate the problem keys, hard-abort, fail fast.
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
                if result is _SENTINEL:  # woken up while stop is set
                    break
                bar_parse.update(len(result.records))
                paragraph_total += result.paragraphs_total
                paragraph_succeeded += result.paragraphs_succeeded
                paragraph_failed += result.paragraphs_failed
                wtp_error_total += result.wtp_errors
                if result.failures:  # observable parse failures: count + print the first few
                    failed_total += len(result.failures)
                    bar_parse.set_postfix(failed=failed_total, refresh=False)
                    for key, err in result.failures:
                        line = f"[parse-fail] {key_column}={key}: {err}"
                        if failure_logger is not None:
                            failure_logger(line)
                        if logged < LOG_LIMIT:
                            tqdm.write(line)
                            logged += 1
                            if logged == LOG_LIMIT:
                                tqdm.write("[parse-fail] ... further failures not printed individually; see the final summary for the total.")
                if not safe_put(write_q, (batch_id, result.records)):
                    break
        except BaseException as exc:  # noqa: BLE001
            fail(exc)
        finally:
            safe_put(write_q, _SENTINEL)

    def writer_loop() -> None:
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
        """Run one cleanup action; on failure only collect it, never raise -- avoids masking the primary exception / interrupting other cleanup."""
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
        # Cleanup is isolated step by step: one failing step does not affect the
        # others; exceptions are collected and adjudicated together, and a cleanup
        # exception must never mask the real root cause (first_error / an
        # exception propagating out of the try).
        if hard_abort.is_set():
            # Worker wedged: force-kill the processes and do not wait, otherwise shutdown(wait=True) would hang too.
            _safe(lambda: terminate_workers(executor))
            _safe(lambda: executor.shutdown(wait=False, cancel_futures=True))
        else:
            # Normal / soft error: cancel_futures cancels queued tasks so we need not wait for the whole queue to drain.
            _safe(lambda: executor.shutdown(wait=True, cancel_futures=True))
        _safe(processed_writer.close)  # includes the final flush; a failure here is a real error (should propagate when there is no root cause)
        if raw_writer is not None:
            _safe(raw_writer.close)
        _safe(source_close)
        for bar in (bar_write, bar_parse, bar_read):
            _safe(bar.close)
        if bar_raw is not None:
            _safe(bar_raw.close)

    # Adjudicate the primary cause: if first_error exists, it wins (cleanup
    # exceptions are downgraded to warnings); otherwise a cleanup exception (e.g.
    # a failed final flush = possible data loss) is itself the primary cause and
    # must propagate.
    primary = first_error[0] if first_error else (cleanup_errors[0] if cleanup_errors else None)
    for exc in cleanup_errors:
        if exc is not primary:
            print(f"WARNING: cleanup-stage exception (does not mask the primary cause): {type(exc).__name__}: {exc}")
    if primary is not None:
        raise primary
    print(
        "Paragraph stats: "
        f"total={paragraph_total}, succeeded={paragraph_succeeded}, "
        f"failed={paragraph_failed}, wtp_lua_errors={wtp_error_total}"
    )
    return int(bar_write.n), failed_total
