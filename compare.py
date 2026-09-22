from urllib.parse import quote
import html
import re

import pyodbc
import streamlit as st
import streamlit.components.v1 as components


# ============================================================
# Streamlit
# ============================================================

st.set_page_config(
    page_title="SimpleWiki Paragraph Viewer",
    layout="wide",
)

st.markdown(
    """
    <style>
    header[data-testid="stHeader"] { display: none; }
    div[data-testid="stToolbar"] { display: none; }
    #MainMenu { visibility: hidden; }

    .block-container {
        max-width: 100%;
        padding-top: 0.8rem;
        padding-left: 1rem;
        padding-right: 1rem;
        padding-bottom: 0.5rem;
    }

    div[data-testid="stForm"] {
        border: 0 !important;
        padding: 0 !important;
    }

    .page-meta {
        font-size: 14px;
        margin: 0.1rem 0 0.35rem 0;
        color: #666;
    }

    .wiki-link {
        font-size: 13px;
        margin-bottom: 0.35rem;
        word-break: break-all;
    }

    .left-stack {
        height: 650px;
        display: flex;
        flex-direction: column;
        gap: 8px;
        min-height: 0;
    }

    .paragraph-container {
        flex: 1 1 auto;
        min-height: 0;
        box-sizing: border-box;
        overflow-y: auto;
        overflow-x: hidden;
        padding: 0.8rem 1rem;
        border: 1px solid rgba(128, 128, 128, 0.35);
        border-radius: 0.5rem;
        font-family: Consolas, "Courier New", monospace;
        font-size: 16px;
        line-height: 1.2;
        background: transparent;
    }

    .content-details {
        flex: 0 0 38px;
        min-height: 38px;
        max-height: 38px;
        box-sizing: border-box;
        overflow: hidden;
        border: 1px solid rgba(128, 128, 128, 0.35);
        border-radius: 0.5rem;
        background: transparent;
        font-family: Consolas, "Courier New", monospace;
        font-size: 16px;
        line-height: 1.2;
    }

    .content-details[open] {
        flex: 0 0 300px;
        height: 300px;
        min-height: 300px;
        max-height: 300px;
        display: flex;
        flex-direction: column;
        overflow: hidden;
    }

    .content-details > summary {
        flex: 0 0 38px;
        height: 38px;
        box-sizing: border-box;
        display: flex;
        align-items: center;
        padding: 0 0.8rem;
        cursor: pointer;
        user-select: none;
        font-family: inherit;
        font-size: 14px;
        line-height: 1;
    }

    .content-details > summary::marker {
        content: "";
    }

    .content-details > summary::before {
        content: "▸";
        display: inline-block;
        width: 1rem;
        margin-right: 0.25rem;
        color: #666;
        font-size: 0.9rem;
        line-height: 1;
        transform-origin: 45% 50%;
        transition: transform 0.15s ease;
    }

    .content-details[open] > summary::before {
        transform: rotate(90deg);
    }

    .content-container {
        flex: 1 1 auto;
        height: auto;
        min-height: 0;
        box-sizing: border-box;
        overflow-y: auto !important;
        overflow-x: hidden;
        overscroll-behavior: contain;
        padding: 0.6rem 1rem 0.8rem 1rem;
        white-space: pre-wrap;
        overflow-wrap: anywhere;
        word-break: break-word;
        font-family: inherit;
        font-size: 16px;
        line-height: 1.2;
    }

    .paragraph-block {
        white-space: pre-wrap;
        overflow-wrap: anywhere;
        word-break: break-word;
    }

    .paragraph-separator {
        height: 1px;
        background-color: #ff8c00;
        margin: 2px 0;
        padding: 0;
        border: 0;
    }

    .template-marker {
        color: #0066cc;
    }

    .arrow-link-marker {
        color: #800080;
    }

    .parse-error-marker {
        display: inline-block;
        margin-right: 5px;
        color: #d93025;
        font-weight: 700;
        line-height: 1;
        cursor: help;
    }

    .wtp-stage-container {
        height: 650px;
        box-sizing: border-box;
        overflow-y: auto;
        overflow-x: hidden;
        padding: 0.75rem 0.9rem;
        border: 1px solid rgba(128, 128, 128, 0.35);
        border-radius: 0.5rem;
        background: transparent;
        font-family: Consolas, "Courier New", monospace;
        font-size: 14px;
        line-height: 1.3;
    }

    .wtp-stage-row + .wtp-stage-row {
        margin-top: 0.9rem;
    }

    .wtp-stage-header {
        color: inherit;
        white-space: pre-wrap;
        overflow-wrap: anywhere;
        word-break: break-word;
    }

    .wtp-stage-body {
        color: #000000;
        white-space: pre-wrap;
        overflow-wrap: anywhere;
        word-break: break-word;
    }

    </style>
    """,
    unsafe_allow_html=True,
)


# ============================================================
# SQL Server 配置
# ============================================================

DB_CONFIG = {
    "host": "localhost",
    "port": 1433,
    "user": "sa",
    "password": "Wang342688",
    "database": "Dev0921",
    "schema": "dbo",
}

PARAGRAPH_TABLE = "wiki_paragraph_[20260801]"
SENTENCE_TABLE = "wiki_sentence_[20260801]"
LATEST_TABLE = "wiki_latest_[20260801]"
INTERMEDIATE_TABLE = "wiki_wtp_intermediate_[20260801]"

VIEW_OPTIONS = {
    "页面对照": "compare",
    "WTP 中间结果": "wtp",
}

LEFT_SOURCE_OPTIONS = {
    "Paragraph": "paragraph",
    "Sentence": "sentence",
}


# ============================================================
# SQL Server
# ============================================================

def quote_identifier(identifier):
    """Quote one SQL Server identifier, escaping closing brackets."""
    if not identifier:
        raise ValueError("SQL identifier must not be empty")
    return "[" + identifier.replace("]", "]]") + "]"


def qualified_table(table_name):
    """Return a safely quoted schema-qualified SQL Server table name."""
    return f"{quote_identifier(DB_CONFIG['schema'])}.{quote_identifier(table_name)}"

@st.cache_data(show_spinner=False)
def get_sql_driver():
    drivers = pyodbc.drivers()

    preferred = [
        "ODBC Driver 18 for SQL Server",
        "ODBC Driver 17 for SQL Server",
        "SQL Server",
    ]

    for driver in preferred:
        if driver in drivers:
            return driver

    if drivers:
        return drivers[-1]

    raise RuntimeError(
        "没有检测到 SQL Server ODBC Driver。"
        "请安装 Microsoft ODBC Driver 18 for SQL Server。"
    )


def open_sql_connection():
    driver = get_sql_driver()

    conn_str = (
        f"DRIVER={{{driver}}};"
        f"SERVER={DB_CONFIG['host']},{DB_CONFIG['port']};"
        f"DATABASE={DB_CONFIG['database']};"
        f"UID={DB_CONFIG['user']};"
        f"PWD={DB_CONFIG['password']};"
        "Encrypt=no;"
        "TrustServerCertificate=yes;"
    )

    return pyodbc.connect(conn_str, timeout=10)


@st.cache_data(show_spinner=False, ttl=60)
def fetch_page_paragraphs(page_id):
    """
    一次性加载指定 page_id 的全部段落。

    排序规则固定为：
        section_no ASC,
        paragraph_no ASC,
        id ASC

    id 仅用于在 section_no / paragraph_no 相同的极端情况下保证稳定顺序。
    """
    sql = f"""
    SELECT
        id,
        revision_id,
        page_id,
        section_no,
        paragraph_no,
        toc,
        [text],
        page_title,
        raw_wikitext,
        parse_error
    FROM {qualified_table(PARAGRAPH_TABLE)}
    WHERE page_id = ?
    ORDER BY
        section_no ASC,
        paragraph_no ASC,
        id ASC;
    """

    with open_sql_connection() as conn:
        cursor = conn.cursor()
        rows = cursor.execute(sql, int(page_id)).fetchall()

    result = []
    for row in rows:
        result.append(
            {
                "id": int(row.id),
                "revision_id": int(row.revision_id),
                "page_id": int(row.page_id),
                "section_no": int(row.section_no),
                "paragraph_no": int(row.paragraph_no),
                "toc": row.toc,
                "text": row.text or "",
                "page_title": row.page_title or "",
                "raw_wikitext": row.raw_wikitext or "",
                "parse_error": row.parse_error,
            }
        )

    return result


@st.cache_data(show_spinner=False, ttl=60)
def fetch_page_sentences(page_id):
    """
    一次性加载指定 page_id 的全部句子。

    排序规则固定为：
        section_no ASC,
        paragraph_no ASC,
        sentence_no ASC,
        id ASC
    """
    sql = f"""
    SELECT
        id,
        revision_id,
        page_id,
        page_title,
        section_no,
        paragraph_no,
        sentence_no,
        toc,
        raw_text,
        [text]
    FROM {qualified_table(SENTENCE_TABLE)}
    WHERE page_id = ?
    ORDER BY
        section_no ASC,
        paragraph_no ASC,
        sentence_no ASC,
        id ASC;
    """

    with open_sql_connection() as conn:
        cursor = conn.cursor()
        rows = cursor.execute(sql, int(page_id)).fetchall()

    result = []
    for row in rows:
        result.append(
            {
                "id": int(row.id),
                "revision_id": int(row.revision_id),
                "page_id": int(row.page_id),
                "page_title": row.page_title or "",
                "section_no": int(row.section_no),
                "paragraph_no": int(row.paragraph_no),
                "sentence_no": int(row.sentence_no),
                "toc": row.toc,
                "raw_text": row.raw_text or "",
                "text": row.text or "",
                "parse_error": None,
            }
        )

    return result


@st.cache_data(show_spinner=False, ttl=60)
def fetch_latest_content(page_id):
    """读取当前版本 latest 表中指定 page_id 的最新一条 content。"""
    sql = f"""
    SELECT TOP (1)
        page_id,
        revision_id,
        page_title,
        content
    FROM {qualified_table(LATEST_TABLE)}
    WHERE page_id = ?
    ORDER BY revision_id DESC;
    """

    with open_sql_connection() as conn:
        cursor = conn.cursor()
        row = cursor.execute(sql, int(page_id)).fetchone()

    if row is None:
        return None

    return {
        "page_id": int(row.page_id),
        "revision_id": int(row.revision_id) if row.revision_id is not None else 0,
        "page_title": row.page_title or "",
        "content": row.content or "",
    }


@st.cache_data(show_spinner=False, ttl=60)
def fetch_wtp_intermediate(page_id):
    """读取当前 page_id 的 WTP 输入、展开结果与 paragraph.text 对照。"""
    sql = f"""
    SELECT
        i.revision_id,
        i.page_id,
        COALESCE(p.page_title, i.page_title) AS page_title,
        i.section_no,
        i.paragraph_no,
        COALESCE(p.toc, i.toc) AS toc,
        i.wtp_input,
        i.expanded_wikitext,
        p.[text] AS [text],
        i.parse_error AS wtp_parse_error,
        p.parse_error AS paragraph_parse_error
    FROM {qualified_table(INTERMEDIATE_TABLE)} AS i
    INNER JOIN {qualified_table(PARAGRAPH_TABLE)} AS p
        ON p.revision_id = i.revision_id
       AND p.page_id = i.page_id
       AND p.section_no = i.section_no
       AND p.paragraph_no = i.paragraph_no
    WHERE i.page_id = ?
    ORDER BY
        i.revision_id ASC,
        i.section_no ASC,
        i.paragraph_no ASC;
    """

    with open_sql_connection() as conn:
        cursor = conn.cursor()
        rows = cursor.execute(sql, int(page_id)).fetchall()

    result = []
    for row in rows:
        result.append(
            {
                "revision_id": int(row.revision_id),
                "page_id": int(row.page_id),
                "page_title": row.page_title or "",
                "section_no": int(row.section_no),
                "paragraph_no": int(row.paragraph_no),
                "toc": row.toc or "",
                "wtp_input": row.wtp_input or "",
                "expanded_wikitext": row.expanded_wikitext or "",
                "text": row.text or "",
                "wtp_parse_error": row.wtp_parse_error,
                "paragraph_parse_error": row.paragraph_parse_error,
            }
        )

    return result


def get_navigation_table():
    """WTP 视图按 intermediate 表翻页；普通视图按 paragraph 表翻页。"""
    view_mode = VIEW_OPTIONS.get(st.session_state.get("view_mode", "页面对照"), "compare")
    return INTERMEDIATE_TABLE if view_mode == "wtp" else PARAGRAPH_TABLE


@st.cache_data(show_spinner=False, ttl=60)
def get_neighbor_page_id(page_id, direction, table_name):
    if direction == "prev":
        comparator = "<"
        order = "DESC"
    elif direction == "next":
        comparator = ">"
        order = "ASC"
    else:
        raise ValueError("direction 必须是 prev 或 next")

    if table_name not in {PARAGRAPH_TABLE, INTERMEDIATE_TABLE}:
        raise ValueError(f"不允许的导航表：{table_name}")

    # 直接在 page_id 上顺序查找，避免 DISTINCT / GROUP BY 对大表做额外聚合。
    sql = f"""
    SELECT TOP (1)
        page_id
    FROM {qualified_table(table_name)}
    WHERE page_id {comparator} ?
    ORDER BY page_id {order};
    """

    with open_sql_connection() as conn:
        cursor = conn.cursor()
        row = cursor.execute(sql, int(page_id)).fetchone()

    if row is None:
        return None

    return int(row.page_id)


@st.cache_data(show_spinner=False, ttl=60)
def get_page_id_bounds(table_name):
    if table_name not in {PARAGRAPH_TABLE, INTERMEDIATE_TABLE}:
        raise ValueError(f"不允许的导航表：{table_name}")

    sql = f"""
    SELECT
        MIN(page_id) AS min_page_id,
        MAX(page_id) AS max_page_id
    FROM {qualified_table(table_name)};
    """

    with open_sql_connection() as conn:
        cursor = conn.cursor()
        row = cursor.execute(sql).fetchone()

    if row is None or row.min_page_id is None or row.max_page_id is None:
        return None

    return int(row.min_page_id), int(row.max_page_id)


def get_random_page_id(table_name):
    """
    随机生成 page_id 区间位置，再向后寻找第一条有效记录。
    如果随机点后面没有记录，则回绕到最小 page_id。
    """
    if table_name not in {PARAGRAPH_TABLE, INTERMEDIATE_TABLE}:
        raise ValueError(f"不允许的导航表：{table_name}")

    bounds = get_page_id_bounds(table_name)
    if bounds is None:
        return None

    min_page_id, max_page_id = bounds

    random_sql = """
    SELECT CAST(
        ? + FLOOR(RAND(CHECKSUM(NEWID())) * (? - ? + 1.0))
        AS BIGINT
    ) AS random_page_id;
    """

    candidate_sql = f"""
    SELECT TOP (1)
        page_id
    FROM {qualified_table(table_name)}
    WHERE page_id >= ?
    ORDER BY page_id ASC;
    """

    fallback_sql = f"""
    SELECT TOP (1)
        page_id
    FROM {qualified_table(table_name)}
    ORDER BY page_id ASC;
    """

    with open_sql_connection() as conn:
        cursor = conn.cursor()

        random_row = cursor.execute(
            random_sql,
            min_page_id,
            max_page_id,
            min_page_id,
        ).fetchone()

        target_page_id = int(random_row.random_page_id)
        row = cursor.execute(candidate_sql, target_page_id).fetchone()

        if row is None:
            row = cursor.execute(fallback_sql).fetchone()

    if row is None:
        return None

    return int(row.page_id)


# ============================================================
# 显示
# ============================================================

def get_page_title(paragraphs, page_id):
    """优先使用第一条非空 page_title。"""
    for item in paragraphs:
        title = (item.get("page_title") or "").strip()
        if title:
            return title
    return str(page_id)


def text_to_safe_html(text):
    """
    将普通文本安全转换为 HTML。

    先进行 HTML 转义，再显式把换行转换为 <br>。
    这样行首的 #、##、- 等字符不会再被 st.markdown()
    当成 Markdown 标题或列表语法解析。
    """
    escaped = html.escape(text or "")
    escaped = escaped.replace("\r\n", "\n").replace("\r", "\n")
    return escaped.replace("\n", "<br>")


def highlight_template_markers(text, sentence_mode=False):
    """
    仅用于左上正文显示。

    高亮规则（Paragraph / Sentence 一致）：
    1. [[<内容>↓<内容>]] 整体标紫色；
    2. ♣ ♣ ♣ <type>-<page_id>-<seq> ♣ ♣ ♣ 整体标蓝色；
    3. 其他 Template:<name>-<id>-<id> 标蓝色。

    三个梅花标记结构优先于普通 Template 标记匹配，
    整个 ♣ ♣ ♣ ... ♣ ♣ ♣ 结构都会显示为蓝色。
    紫色结构仍然优先整段匹配。
    sentence_mode 参数保留用于兼容现有调用，但不再改变该高亮规则。
    同时把换行转换为 <br>，避免行首 # 被 Markdown 解释为标题。
    """
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    escaped = html.escape(normalized)

    pattern = re.compile(
        r"(?P<arrow>\[\[[^\[\]\r\n]*?↓[^\[\]\r\n]*?\]\])"
        r"|(?P<club>♣\s*♣\s*♣\s*[^♣\r\n<>]+?-\d+-\d+\s*♣\s*♣\s*♣)"
        r"|(?P<template>Template:[^\r\n<>]+?-\d+-\d+)",
        flags=re.I,
    )

    def repl(match):
        if match.group("arrow") is not None:
            return f'<span class="arrow-link-marker">{match.group("arrow")}</span>'
        if match.group("club") is not None:
            return f'<span class="template-marker">{match.group("club")}</span>'
        return f'<span class="template-marker">{match.group("template")}</span>'

    highlighted = pattern.sub(repl, escaped)
    return highlighted.replace("\n", "<br>")


def build_left_stack_html(paragraphs, content, sentence_mode=False):
    # 独立组件：避免 st.markdown + details 的滚动问题。
    blocks = []

    for index, item in enumerate(paragraphs):
        text = highlight_template_markers(
            item.get("text") or "",
            sentence_mode=sentence_mode,
        )

        error_marker = ""
        if item.get("parse_error") is not None:
            error_marker = (
                '<span class="parse-error-marker" '
                'title="该段存在 parse_error">⚠</span>'
            )

        blocks.append(
            '<div class="paragraph-block">'
            f'{error_marker}{text}'
            '</div>'
        )

        if index < len(paragraphs) - 1:
            blocks.append('<div class="paragraph-separator"></div>')

    paragraph_html = "".join(blocks)
    content_html = text_to_safe_html(content)

    template = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
    html, body {
        width: 100%;
        height: 100%;
        margin: 0;
        padding: 0;
        overflow: hidden;
        background: transparent;
    }
    * { box-sizing: border-box; }
    .left-stack {
        width: 100%;
        height: 650px;
        display: flex;
        flex-direction: column;
        gap: 8px;
        min-height: 0;
        overflow: hidden;
        font-family: Consolas, "Courier New", monospace;
        font-size: 16px;
        line-height: 1.2;
    }
    .paragraph-container {
        flex: 1 1 auto;
        min-height: 0;
        overflow-y: auto;
        overflow-x: hidden;
        overscroll-behavior: contain;
        scrollbar-gutter: stable;
        padding: 0.8rem 1rem;
        border: 1px solid rgba(128, 128, 128, 0.35);
        border-radius: 0.5rem;
        background: transparent;
    }
    .paragraph-block {
        white-space: pre-wrap;
        overflow-wrap: anywhere;
        word-break: break-word;
    }
    .paragraph-separator {
        height: 1px;
        background-color: #ff8c00;
        margin: 2px 0;
        padding: 0;
        border: 0;
    }
    .template-marker { color: #0066cc; }
    .arrow-link-marker { color: #800080; }
    .parse-error-marker {
        display: inline-block;
        margin-right: 5px;
        color: #d93025;
        font-weight: 700;
        line-height: 1;
        cursor: help;
    }
    .content-panel {
        flex: 0 0 38px;
        height: 38px;
        min-height: 38px;
        display: flex;
        flex-direction: column;
        overflow: hidden;
        border: 1px solid rgba(128, 128, 128, 0.35);
        border-radius: 0.5rem;
        background: transparent;
    }
    .content-panel.open {
        flex: 0 0 300px;
        height: 300px;
        min-height: 300px;
    }
    .content-toggle {
        flex: 0 0 36px;
        width: 100%;
        height: 36px;
        border: 0;
        margin: 0;
        padding: 0 0.8rem;
        display: flex;
        align-items: center;
        gap: 0.25rem;
        background: transparent;
        color: inherit;
        font: inherit;
        font-size: 14px;
        line-height: 1;
        text-align: left;
        cursor: pointer;
        user-select: none;
    }
    .content-toggle:hover { background: rgba(128, 128, 128, 0.08); }
    .content-arrow {
        display: inline-block;
        width: 1rem;
        color: #666;
        font-size: 0.9rem;
        line-height: 1;
        transform-origin: 45% 50%;
        transition: transform 0.12s ease;
    }
    .content-panel.open .content-arrow { transform: rotate(90deg); }
    .content-container {
        flex: 1 1 auto;
        min-height: 0;
        overflow-y: scroll;
        overflow-x: hidden;
        overscroll-behavior: contain;
        scrollbar-gutter: stable;
        touch-action: pan-y;
        padding: 0.6rem 1rem 0.8rem 1rem;
        white-space: pre-wrap;
        overflow-wrap: anywhere;
        word-break: break-word;
    }
    .content-panel:not(.open) .content-container { display: none; }
</style>
</head>
<body>
<div class="left-stack">
    <div class="paragraph-container">__PARAGRAPH_HTML__</div>
    <div id="contentPanel" class="content-panel">
        <button id="contentToggle" class="content-toggle" type="button"
                aria-expanded="false" aria-controls="contentBody">
            <span class="content-arrow">▸</span>
            <span>原始 content</span>
        </button>
        <div id="contentBody" class="content-container">__CONTENT_HTML__</div>
    </div>
</div>
<script>
(function () {
    const panel = document.getElementById('contentPanel');
    const toggle = document.getElementById('contentToggle');
    const body = document.getElementById('contentBody');

    toggle.addEventListener('click', function () {
        const isOpen = panel.classList.toggle('open');
        toggle.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
        if (isOpen) {
            requestAnimationFrame(function () { body.scrollTop = 0; });
        }
    });

    body.addEventListener('wheel', function (event) {
        if (!panel.classList.contains('open')) return;
        const maxScroll = body.scrollHeight - body.clientHeight;
        if (maxScroll <= 0) return;
        const next = Math.max(0, Math.min(maxScroll, body.scrollTop + event.deltaY));
        if (next !== body.scrollTop) {
            event.preventDefault();
            body.scrollTop = next;
        }
    }, { passive: false });
})();
</script>
</body>
</html>"""

    return (
        template
        .replace("__PARAGRAPH_HTML__", paragraph_html)
        .replace("__CONTENT_HTML__", content_html)
    )


def render_left_stack(paragraphs, content, sentence_mode=False):
    components.html(
        build_left_stack_html(
            paragraphs,
            content,
            sentence_mode=sentence_mode,
        ),
        height=650,
        scrolling=False,
    )


def build_wtp_interactive_html(rows):
    """
    构建 WTP 三列联动查看器。

    点击任意列的段落标题后，三列都会滚动到同一个
    revision_id + section_no + paragraph_no 对应位置。
    """
    stage_defs = [
        ("wtp_input", "wtp_input"),
        ("expanded_wikitext", "expanded_wikitext"),
        ("text", "text"),
    ]

    columns = []
    for field, label in stage_defs:
        blocks = []
        for row_index, row in enumerate(rows):
            toc = f" | {row['toc']}" if row.get("toc") else ""
            header = (
                f"=== section {row['section_no']} | "
                f"paragraph {row['paragraph_no']}{toc} ==="
            )
            body = row.get(field) or ""

            # row_index 直接对应三列中的同一条联查记录；同时附带数据库键便于调试。
            sync_key = (
                f"{row.get('revision_id', 0)}-"
                f"{row['section_no']}-"
                f"{row['paragraph_no']}-"
                f"{row_index}"
            )

            blocks.append(
                '<div class="wtp-stage-row" '
                f'data-sync-key="{html.escape(sync_key, quote=True)}">'
                '<div class="wtp-stage-header" '
                f'data-sync-key="{html.escape(sync_key, quote=True)}" '
                'title="点击后让三列跳转到同一段">'
                + text_to_safe_html(header)
                + '</div>'
                '<div class="wtp-stage-body">'
                + text_to_safe_html(body)
                + '</div>'
                '</div>'
            )

        columns.append(
            '<div class="wtp-column">'
            f'<div class="wtp-column-label">{html.escape(label)}</div>'
            '<div class="wtp-stage-container">'
            + "".join(blocks)
            + '</div>'
            '</div>'
        )

    template = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
    html, body {
        margin: 0;
        padding: 0;
        background: transparent;
        font-family: sans-serif;
    }

    .wtp-grid {
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: 16px;
        width: 100%;
    }

    .wtp-column {
        min-width: 0;
    }

    .wtp-column-label {
        margin: 0 0 8px 0;
        font-weight: 700;
        font-size: 14px;
        line-height: 1.2;
        color: inherit;
    }

    .wtp-stage-container {
        height: 650px;
        box-sizing: border-box;
        overflow-y: auto;
        overflow-x: hidden;
        padding: 0.75rem 0.9rem;
        border: 1px solid rgba(128, 128, 128, 0.35);
        border-radius: 0.5rem;
        background: transparent;
        font-family: Consolas, "Courier New", monospace;
        font-size: 14px;
        line-height: 1.3;
        scroll-behavior: smooth;
    }

    .wtp-stage-row + .wtp-stage-row {
        margin-top: 0.9rem;
    }

    .wtp-stage-header {
        color: #0066cc;
        font-weight: 600;
        white-space: pre-wrap;
        overflow-wrap: anywhere;
        word-break: break-word;
        cursor: pointer;
        user-select: none;
    }

    .wtp-stage-header:hover {
        text-decoration: underline;
    }

    .wtp-stage-body {
        color: #000000;
        white-space: pre-wrap;
        overflow-wrap: anywhere;
        word-break: break-word;
    }
</style>
</head>
<body>
<div class="wtp-grid">
__WTP_COLUMNS__
</div>
<script>
(function () {
    const headers = document.querySelectorAll('.wtp-stage-header[data-sync-key]');

    function scrollRowToTop(container, row) {
        if (!container || !row) return;
        const containerRect = container.getBoundingClientRect();
        const rowRect = row.getBoundingClientRect();
        const targetTop = container.scrollTop + (rowRect.top - containerRect.top);
        container.scrollTo({ top: targetTop, behavior: 'smooth' });
    }

    headers.forEach(function (header) {
        header.addEventListener('click', function () {
            const key = header.dataset.syncKey;
            document.querySelectorAll('.wtp-stage-container').forEach(function (container) {
                const candidates = container.querySelectorAll('.wtp-stage-row[data-sync-key]');
                let target = null;
                for (const row of candidates) {
                    if (row.dataset.syncKey === key) {
                        target = row;
                        break;
                    }
                }
                scrollRowToTop(container, target);
            });
        });
    });
})();
</script>
</body>
</html>"""

    return template.replace("__WTP_COLUMNS__", "".join(columns))


def render_wtp_view(rows):
    """三列显示 WTP 输入、展开结果和最终 text，并支持按段落标题联动跳转。"""
    components.html(
        build_wtp_interactive_html(rows),
        height=690,
        scrolling=False,
    )

    errors = [
        row for row in rows
        if row.get("wtp_parse_error") is not None
        or row.get("paragraph_parse_error") is not None
    ]

    if errors:
        with st.expander(f"Parse Errors ({len(errors)})", expanded=False):
            for row in errors:
                st.caption(
                    f"section_no: {row['section_no']} ｜ "
                    f"paragraph_no: {row['paragraph_no']}"
                )
                values = []
                if row.get("wtp_parse_error") is not None:
                    values.append("[WTP] " + str(row["wtp_parse_error"]))
                if row.get("paragraph_parse_error") is not None:
                    values.append("[Paragraph] " + str(row["paragraph_parse_error"]))
                st.code("\n".join(values), language="text")


def get_parse_error_items(paragraphs):
    """返回当前 page_id 中 parse_error IS NOT NULL 的段落。"""
    return [
        item for item in paragraphs
        if item.get("parse_error") is not None
    ]


# ============================================================
# Session State
# ============================================================

if "current_page_id" not in st.session_state:
    st.session_state.current_page_id = None

if "page_id_input" not in st.session_state:
    st.session_state.page_id_input = 1

if "view_mode" not in st.session_state:
    st.session_state.view_mode = "页面对照"

if "left_source" not in st.session_state:
    st.session_state.left_source = "Paragraph"


# ============================================================
# Callbacks
# ============================================================

def search_page():
    st.session_state.current_page_id = int(st.session_state.page_id_input)


def go_prev_page():
    current = st.session_state.current_page_id
    if current is None:
        return

    target = get_neighbor_page_id(current, "prev", get_navigation_table())
    if target is not None:
        st.session_state.current_page_id = target
        st.session_state.page_id_input = target


def go_next_page():
    current = st.session_state.current_page_id
    if current is None:
        return

    target = get_neighbor_page_id(current, "next", get_navigation_table())
    if target is not None:
        st.session_state.current_page_id = target
        st.session_state.page_id_input = target


def go_random_page():
    target = get_random_page_id(get_navigation_table())
    if target is not None:
        st.session_state.current_page_id = target
        st.session_state.page_id_input = target


# ============================================================
# 顶部控制区
# ============================================================

view_col, source_col, search_form_col, random_page_col, prev_page_col, next_page_col = st.columns(
    [1.35, 1.15, 3.6, 0.9, 0.9, 0.9],
    gap="small",
)

with view_col:
    st.selectbox(
        "视图模式",
        list(VIEW_OPTIONS.keys()),
        key="view_mode",
        label_visibility="collapsed",
    )

with source_col:
    st.selectbox(
        "左上数据来源",
        list(LEFT_SOURCE_OPTIONS.keys()),
        key="left_source",
        label_visibility="collapsed",
        disabled=(VIEW_OPTIONS[st.session_state.view_mode] == "wtp"),
    )

with search_form_col:
    with st.form("page_search_form", clear_on_submit=False):
        input_col, submit_col = st.columns([3.1, 1.0], gap="small")

        with input_col:
            st.number_input(
                "page_id",
                min_value=1,
                step=1,
                key="page_id_input",
                label_visibility="collapsed",
                placeholder="输入 page_id，回车搜索",
            )

        with submit_col:
            st.form_submit_button(
                "搜索",
                use_container_width=True,
                on_click=search_page,
            )

with random_page_col:
    st.button(
        "随机",
        use_container_width=True,
        on_click=go_random_page,
    )

with prev_page_col:
    st.button(
        "上一页",
        use_container_width=True,
        on_click=go_prev_page,
        disabled=(st.session_state.current_page_id is None),
    )

with next_page_col:
    st.button(
        "下一页",
        use_container_width=True,
        on_click=go_next_page,
        disabled=(st.session_state.current_page_id is None),
    )


# ============================================================
# 当前页面
# ============================================================

if st.session_state.current_page_id is None:
    st.info("输入 page_id 后点击“搜索”，或直接按 Enter；也可以点击“随机”。")
    st.stop()

current_page_id = int(st.session_state.current_page_id)
view_mode = VIEW_OPTIONS[st.session_state.view_mode]

if view_mode == "wtp":
    wtp_rows = fetch_wtp_intermediate(current_page_id)
    if not wtp_rows:
        st.error(
            f"没有在 {DB_CONFIG['schema']}.{INTERMEDIATE_TABLE} 与 "
            f"{DB_CONFIG['schema']}.{PARAGRAPH_TABLE} 的联查结果中找到 "
            f"page_id = {current_page_id}"
        )
        st.stop()

    render_wtp_view(wtp_rows)
    st.stop()

# ============================================================
# 页面对照视图
# ============================================================

paragraphs = fetch_page_paragraphs(current_page_id)
latest_page = fetch_latest_content(current_page_id)

left_source_mode = LEFT_SOURCE_OPTIONS[st.session_state.left_source]
if left_source_mode == "sentence":
    display_items = fetch_page_sentences(current_page_id)
    display_table = SENTENCE_TABLE
else:
    display_items = paragraphs
    display_table = PARAGRAPH_TABLE

if not display_items:
    st.error(
        f"没有在 {DB_CONFIG['schema']}.{display_table} 中找到 "
        f"page_id = {current_page_id}"
    )
    st.stop()

# page_title 优先取 paragraph；若该页仅 sentence 有数据，则退回 sentence。
title_source = paragraphs if paragraphs else display_items
page_title = get_page_title(title_source, current_page_id)

safe_title = quote(page_title.replace(" ", "_"), safe="/:")
wiki_url = "https://simple.wikipedia.org/wiki/" + safe_title


# ============================================================
# 左右分栏
# ============================================================

left, right = st.columns([1, 1], gap="medium")


# ------------------------------------------------------------
# 左上：可切换当前版本 paragraph.text / sentence.text
# 左下：当前版本 latest.content 原文
# ------------------------------------------------------------

with left:
    latest_content = latest_page["content"] if latest_page is not None else ""
    render_left_stack(
        display_items,
        latest_content,
        sentence_mode=(left_source_mode == "sentence"),
    )

    parse_error_items = get_parse_error_items(paragraphs)
    if parse_error_items:
        with st.expander(f"Parse Errors ({len(parse_error_items)})", expanded=False):
            for item in parse_error_items:
                st.caption(
                    f'section_no: {item["section_no"]} ｜ '
                    f'paragraph_no: {item["paragraph_no"]}'
                )
                error_text = item.get("parse_error")
                st.code(
                    "" if error_text is None else str(error_text),
                    language="text",
                )


# ------------------------------------------------------------
# 右：Simple Wikipedia
# ------------------------------------------------------------

with right:
    st.subheader("Wikipedia", divider=False)

    st.markdown(
        (
            '<div class="wiki-link">'
            f'<a href="{wiki_url}" target="_blank">'
            f'{html.escape(wiki_url)}'
            '</a>'
            '</div>'
        ),
        unsafe_allow_html=True,
    )

    components.iframe(
        wiki_url,
        height=650,
        scrolling=True,
    )

    st.caption("右侧 Wikipedia 地址由当前版本 paragraph.page_title 生成。")
