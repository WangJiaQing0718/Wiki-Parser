from pathlib import Path
import html
import re
from html.parser import HTMLParser

import pyodbc
from wikitextprocessor import Wtp


# ============================================================
# SQL Server
# ============================================================

DB_CONFIG = {
    "host": "localhost",
    "port": 1433,
    "user": "sa",
    "password": "Wang342688",
    "database": "Dev0918",
    "schema": "dbo",
}

PAGE_ID = 600


# ============================================================
# WTP SQLite DB
# 按你当前实际位置修改
# ============================================================

WTP_DB_PATH = Path(
    r"E:\631\Wiki - Fixed\WikiData\simplewiki-20260801-wtp-full.db"
)


# ============================================================
# 输出文件
# ============================================================

OUTPUT_DIR = Path(__file__).resolve().parent

SOURCE_FILE = OUTPUT_DIR / f"page_{PAGE_ID}_source.wiki"
EXPANDED_FILE = OUTPUT_DIR / f"page_{PAGE_ID}_expanded.wiki"
CITE_FILE = OUTPUT_DIR / f"page_{PAGE_ID}_cite_processed.wiki"
TEXT_FILE = OUTPUT_DIR / f"page_{PAGE_ID}_text.txt"
LOG_FILE = OUTPUT_DIR / f"page_{PAGE_ID}_wtp_log.txt"


# ============================================================
# SQL Server
# ============================================================

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
        "没有检测到 SQL Server ODBC Driver"
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

    return pyodbc.connect(
        conn_str,
        timeout=10,
    )


def fetch_page(page_id):
    sql = f"""
    SELECT TOP (1)
        revision_id,
        page_id,
        page_title,
        namespace,
        content
    FROM [{DB_CONFIG['schema']}].[simplewiki_latest]
    WHERE page_id = ?
    ORDER BY revision_id DESC;
    """

    with open_sql_connection() as conn:
        cursor = conn.cursor()

        row = cursor.execute(
            sql,
            page_id,
        ).fetchone()

    if row is None:
        raise RuntimeError(
            f"找不到 page_id={page_id}"
        )

    return {
        "revision_id": row.revision_id,
        "page_id": row.page_id,
        "page_title": row.page_title,
        "namespace": row.namespace,
        "content": row.content or "",
    }


# ============================================================
# WTP 模板钩子
# ============================================================

MEDIAWIKI_MESSAGES = {
    "toc": "Contents",
}


def template_hook(name, args):
    name = name.strip()

    # 避免普通页面解析时 substcheck 误判
    if name.casefold() == "substcheck":
        return ""

    # 兼容部分 MediaWiki 系统消息
    if ":" in name:
        prefix, message_name = name.split(":", 1)

        if prefix.casefold() == "mediawiki":
            return MEDIAWIKI_MESSAGES.get(
                message_name.strip().casefold()
            )

    return None


# ============================================================
# 最小 Cite Processor
#
# WTP 可以把：
#   {{efn|...}}     -> <ref group="lower-alpha">...</ref>
#   {{notelist}}    -> <references group="lower-alpha" />
#   {{reflist}}     -> <references />
#
# 但 WTP 不会继续执行 MediaWiki Extension:Cite 的页面级汇总。
# 这里做一个最小实现：
#   1. 收集 <ref>...</ref>
#   2. 支持 name= 的 named ref / <ref name="x" /> 复用
#   3. 按 group 分类
#   4. 用收集到的内容替换 <references ... />
#   5. 删除正文中的 inline <ref> 标签
#
# 目标是纯文本，不模拟 backlink、HTML id、CSS 等 MediaWiki Cite UI。
# ============================================================


_REF_RE = re.compile(
    r'<ref\b(?P<attrs>[^>]*?)(?:/\s*>|>(?P<content>.*?)</ref\s*>)',
    flags=re.I | re.S,
)

_REFERENCES_SELF_RE = re.compile(
    r'<references\b(?P<attrs>[^>]*)/\s*>',
    flags=re.I | re.S,
)

_REFERENCES_PAIR_RE = re.compile(
    r'<references\b(?P<attrs>[^>]*)>.*?</references\s*>',
    flags=re.I | re.S,
)

_ATTR_RE = re.compile(
    r'''([:\w-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))''',
    flags=re.I,
)


def parse_tag_attrs(attr_text):
    attrs = {}

    for match in _ATTR_RE.finditer(attr_text or ""):
        key = match.group(1).casefold()
        value = next(
            (
                item
                for item in match.groups()[1:]
                if item is not None
            ),
            "",
        )
        attrs[key] = html.unescape(value.strip())

    return attrs


def alpha_label(index, upper=False):
    # 1 -> a, 26 -> z, 27 -> aa
    chars = []
    n = index

    while n > 0:
        n -= 1
        chars.append(chr(ord("a") + n % 26))
        n //= 26

    label = "".join(reversed(chars))

    if upper:
        label = label.upper()

    return label


def process_cite(expanded):
    # 第一遍：先收集所有 named ref 定义
    named_definitions = {}

    for match in _REF_RE.finditer(expanded):
        content = match.group("content")

        if content is None:
            continue

        attrs = parse_tag_attrs(match.group("attrs"))
        group = attrs.get("group", "").strip()
        name = attrs.get("name", "").strip()

        if name:
            named_definitions.setdefault(
                (group, name),
                content.strip(),
            )

    # 第二遍：按页面出现顺序建立 references 列表
    refs_by_group = {}
    seen_named = set()
    ref_occurrences = 0
    unresolved_named = 0

    for match in _REF_RE.finditer(expanded):
        ref_occurrences += 1

        attrs = parse_tag_attrs(match.group("attrs"))
        group = attrs.get("group", "").strip()
        name = attrs.get("name", "").strip()
        content = match.group("content")

        if name:
            key = (group, name)

            if key in seen_named:
                continue

            if content is None:
                content = named_definitions.get(key)

            if content is None:
                unresolved_named += 1
                continue

            seen_named.add(key)

        elif content is None:
            continue

        content = (content or "").strip()

        if not content:
            continue

        refs_by_group.setdefault(group, []).append(content)

    rendered_groups = set()

    def render_references(match):
        attrs = parse_tag_attrs(match.group("attrs"))
        group = attrs.get("group", "").strip()
        refs = refs_by_group.get(group, [])

        rendered_groups.add(group)

        if not refs:
            return ""

        lines = []

        for index, content in enumerate(refs, start=1):
            if group.casefold() == "lower-alpha":
                label = alpha_label(index)
            elif group.casefold() == "upper-alpha":
                label = alpha_label(index, upper=True)
            else:
                label = str(index)

            # ref 内容仍保留 Wikitext / HTML，
            # 后续交给 expanded_wikitext_to_text() 清理。
            lines.append(f"{label}. {content}")

        return "\n" + "\n".join(lines) + "\n"

    cite_processed = _REFERENCES_PAIR_RE.sub(
        render_references,
        expanded,
    )

    cite_processed = _REFERENCES_SELF_RE.sub(
        render_references,
        cite_processed,
    )

    # 已经在 references 位置输出，正文 inline ref 不再重复显示
    cite_processed = _REF_RE.sub(
        "",
        cite_processed,
    )

    stats = {
        "ref_occurrences": ref_occurrences,
        "unique_named_refs": len(seen_named),
        "groups": {
            group: len(refs)
            for group, refs in refs_by_group.items()
        },
        "rendered_groups": sorted(rendered_groups),
        "unresolved_named_refs": unresolved_named,
    }

    return cite_processed, stats


# ============================================================
# Expanded Wikitext -> Text
# ============================================================

def remove_file_links(text):
    """
    删除 [[File:...]] / [[Image:...]]
    """

    pattern = re.compile(
        r"\[\[\s*(?:File|Image):",
        flags=re.I,
    )

    while True:
        match = pattern.search(text)

        if match is None:
            break

        start = match.start()
        pos = match.end()
        depth = 1

        while pos < len(text) - 1:
            chunk = text[pos:pos + 2]

            if chunk == "[[":
                depth += 1
                pos += 2
                continue

            if chunk == "]]":
                depth -= 1
                pos += 2

                if depth == 0:
                    break

                continue

            pos += 1

        if depth != 0:
            break

        text = text[:start] + text[pos:]

    return text


def replace_wikilinks(text):
    """
    [[Beijing]] -> Beijing
    [[Foo|Bar]] -> Bar
    [[:Taiwan]] -> Taiwan
    """

    pattern = re.compile(
        r"\[\[([^\[\]]+)\]\]"
    )

    for _ in range(30):
        changed = False

        def repl(match):
            nonlocal changed

            inner = match.group(1)

            if (
                inner
                .lstrip(":")
                .casefold()
                .startswith("category:")
            ):
                changed = True
                return ""

            parts = inner.split("|")

            if len(parts) >= 2:
                visible = parts[-1]
            else:
                visible = parts[0]

            visible = visible.lstrip(":")

            changed = True
            return visible

        text = pattern.sub(
            repl,
            text,
        )

        if not changed:
            break

    return text


class VisibleTextHTMLParser(HTMLParser):
    """
    保守提取 HTML 可见文字。
    不使用 mwparserfromhell.strip_code()，
    避免误删正常正文。
    """

    BLOCK_TAGS = {
        "p",
        "div",
        "table",
        "tr",
        "td",
        "th",
        "ul",
        "ol",
        "li",
        "dl",
        "dt",
        "dd",
        "section",
        "header",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
    }

    SKIP_TAGS = {
        "style",
        "script",
        "ref",
        "references",
        "templatestyles",
    }

    def __init__(self):
        super().__init__(
            convert_charrefs=True
        )

        self.parts = []
        self.skip_stack = []

    def add_break(self):
        self.parts.append("\n")

    def handle_starttag(self, tag, attrs):
        tag = tag.casefold()

        attr_dict = {
            key.casefold(): value or ""
            for key, value in attrs
        }

        classes = (
            attr_dict
            .get("class", "")
            .casefold()
            .split()
        )

        should_skip = (
            tag in self.SKIP_TAGS
            or "error" in classes
        )

        if should_skip:
            self.skip_stack.append(tag)
            return

        if self.skip_stack:
            return

        if tag == "br":
            self.add_break()

        elif tag == "li":
            self.parts.append("\n")

        elif tag in self.BLOCK_TAGS:
            self.add_break()

    def handle_startendtag(self, tag, attrs):
        tag = tag.casefold()

        if self.skip_stack:
            return

        if tag == "br":
            self.add_break()

    def handle_endtag(self, tag):
        tag = tag.casefold()

        if self.skip_stack:
            if tag == self.skip_stack[-1]:
                self.skip_stack.pop()

            return

        if tag in self.BLOCK_TAGS:
            self.add_break()

    def handle_data(self, data):
        if not self.skip_stack:
            self.parts.append(data)

    def get_text(self):
        return "".join(self.parts)


def expanded_wikitext_to_text(expanded):
    text = expanded

    # --------------------------------------------------------
    # HTML comments
    # --------------------------------------------------------

    text = re.sub(
        r"<!--.*?-->",
        "",
        text,
        flags=re.S,
    )

    # --------------------------------------------------------
    # ref
    # --------------------------------------------------------

    text = re.sub(
        r"<ref\b[^>]*>.*?</ref\s*>",
        "",
        text,
        flags=re.I | re.S,
    )

    text = re.sub(
        r"<ref\b[^>]*/\s*>",
        "",
        text,
        flags=re.I,
    )

    # --------------------------------------------------------
    # File / Image
    # --------------------------------------------------------

    text = remove_file_links(text)

    # --------------------------------------------------------
    # Category
    # --------------------------------------------------------

    text = re.sub(
        r"\[\[\s*:?\s*Category:[^\[\]]*\]\]",
        "",
        text,
        flags=re.I,
    )

    # --------------------------------------------------------
    # Heading
    # --------------------------------------------------------

    text = re.sub(
        r"(?m)^\s*=+\s*(.*?)\s*=+\s*$",
        r"\n\1\n",
        text,
    )

    # --------------------------------------------------------
    # Wiki links
    # --------------------------------------------------------

    text = replace_wikilinks(text)

    # --------------------------------------------------------
    # External links
    # --------------------------------------------------------

    text = re.sub(
        r"\[(?:https?:)?//[^\s\]]+\s+([^\]]+)\]",
        r"\1",
        text,
        flags=re.I,
    )

    text = re.sub(
        r"\[https?://[^\]]+\]",
        "",
        text,
        flags=re.I,
    )

    # --------------------------------------------------------
    # Bold / italic
    # --------------------------------------------------------

    text = text.replace(
        chr(39) * 3,
        "",
    )

    text = text.replace(
        chr(39) * 2,
        "",
    )

    # --------------------------------------------------------
    # Behavior switches
    # --------------------------------------------------------

    text = re.sub(
        r"__[A-Z][A-Z0-9_]*__",
        "",
        text,
    )

    # --------------------------------------------------------
    # HTML -> Text
    # --------------------------------------------------------

    parser = VisibleTextHTMLParser()

    try:
        parser.feed(text)
        parser.close()

        text = parser.get_text()

    except Exception:
        text = re.sub(
            r"<[^>]+>",
            "",
            text,
        )

    # --------------------------------------------------------
    # HTML entity
    # --------------------------------------------------------

    text = html.unescape(text)

    # --------------------------------------------------------
    # Wiki table control symbols
    # --------------------------------------------------------

    text = re.sub(
        r"(?m)^\s*\{\|.*$",
        "",
        text,
    )

    text = re.sub(
        r"(?m)^\s*\|\}\s*$",
        "",
        text,
    )

    text = re.sub(
        r"(?m)^\s*\|-\s*.*$",
        "",
        text,
    )

    # --------------------------------------------------------
    # Whitespace
    # --------------------------------------------------------

    text = text.replace(
        "\xa0",
        " ",
    )

    text = text.replace(
        "\r\n",
        "\n",
    )

    text = text.replace(
        "\r",
        "\n",
    )

    text = re.sub(
        r"[ \t]+",
        " ",
        text,
    )

    lines = [
        line.strip()
        for line in text.splitlines()
    ]

    text = "\n".join(lines)

    text = re.sub(
        r"\n{3,}",
        "\n\n",
        text,
    )

    return text.strip()


# ============================================================
# WTP 整篇展开
# ============================================================

def expand_page(page):
    if not WTP_DB_PATH.exists():
        raise FileNotFoundError(
            f"WTP DB 不存在：{WTP_DB_PATH}"
        )

    wtp = Wtp(
        db_path=WTP_DB_PATH,
        lang_code="en",
        project="wikipedia",
    )

    # 使用真实页面标题
    wtp.start_page(
        page["page_title"]
    )

    expanded = wtp.expand(
        page["content"],
        template_fn=template_hook,
    )

    return {
        "expanded": expanded,
        "errors": list(wtp.errors),
        "warnings": list(wtp.warnings),
        "debugs": list(wtp.debugs),
    }


# ============================================================
# main
# ============================================================

def main():
    print("=" * 100)
    print("读取 SQL Server")
    print("=" * 100)

    page = fetch_page(
        PAGE_ID
    )

    print("page_id      :", page["page_id"])
    print("revision_id  :", page["revision_id"])
    print("page_title   :", page["page_title"])
    print("namespace    :", page["namespace"])
    print("content len  :", len(page["content"]))

    # 保存原始 content
    SOURCE_FILE.write_text(
        page["content"],
        encoding="utf-8",
    )

    print()
    print("=" * 100)
    print("WTP 整篇展开")
    print("=" * 100)

    result = expand_page(
        page
    )

    expanded = result[
        "expanded"
    ]

    EXPANDED_FILE.write_text(
        expanded,
        encoding="utf-8",
    )

    print(
        "expanded len :",
        len(expanded),
    )

    print()
    print("=" * 100)
    print("最小 Cite Processor")
    print("=" * 100)

    cite_processed, cite_stats = process_cite(
        expanded
    )

    CITE_FILE.write_text(
        cite_processed,
        encoding="utf-8",
    )

    print("ref occurrences       :", cite_stats["ref_occurrences"])
    print("unique named refs     :", cite_stats["unique_named_refs"])
    print("groups                :", cite_stats["groups"])
    print("rendered groups       :", cite_stats["rendered_groups"])
    print("unresolved named refs :", cite_stats["unresolved_named_refs"])

    print()
    print("=" * 100)
    print("Cite Processed Wikitext -> Text")
    print("=" * 100)

    plain_text = expanded_wikitext_to_text(
        cite_processed
    )

    TEXT_FILE.write_text(
        plain_text,
        encoding="utf-8",
    )

    # --------------------------------------------------------
    # 日志
    # --------------------------------------------------------

    log_lines = [
        f"page_id: {page['page_id']}",
        f"revision_id: {page['revision_id']}",
        f"page_title: {page['page_title']}",
        f"namespace: {page['namespace']}",
        f"source_length: {len(page['content'])}",
        f"expanded_length: {len(expanded)}",
        f"cite_processed_length: {len(cite_processed)}",
        f"cite_ref_occurrences: {cite_stats['ref_occurrences']}",
        f"cite_unique_named_refs: {cite_stats['unique_named_refs']}",
        f"cite_groups: {cite_stats['groups']}",
        f"cite_rendered_groups: {cite_stats['rendered_groups']}",
        f"cite_unresolved_named_refs: {cite_stats['unresolved_named_refs']}",
        f"text_length: {len(plain_text)}",
        f"errors: {len(result['errors'])}",
        f"warnings: {len(result['warnings'])}",
        f"debugs: {len(result['debugs'])}",
        "",
        "=" * 100,
        "ERRORS",
        "=" * 100,
    ]

    if result["errors"]:
        log_lines.extend(
            str(item)
            for item in result["errors"]
        )
    else:
        log_lines.append("无")

    log_lines += [
        "",
        "=" * 100,
        "WARNINGS",
        "=" * 100,
    ]

    if result["warnings"]:
        log_lines.extend(
            str(item)
            for item in result["warnings"]
        )
    else:
        log_lines.append("无")

    log_lines += [
        "",
        "=" * 100,
        "DEBUGS",
        "=" * 100,
    ]

    if result["debugs"]:
        log_lines.extend(
            str(item)
            for item in result["debugs"]
        )
    else:
        log_lines.append("无")

    LOG_FILE.write_text(
        "\n".join(log_lines),
        encoding="utf-8",
    )

    print("text len     :", len(plain_text))
    print("errors       :", len(result["errors"]))
    print("warnings     :", len(result["warnings"]))
    print("debugs       :", len(result["debugs"]))

    print()
    print("=" * 100)
    print("输出文件")
    print("=" * 100)

    print("原始 Wikitext :", SOURCE_FILE)
    print("展开 Wikitext :", EXPANDED_FILE)
    print("Cite 处理结果 :", CITE_FILE)
    print("最终 Text     :", TEXT_FILE)
    print("WTP 日志      :", LOG_FILE)

    print()
    print("=" * 100)
    print("最终 Text 前 5000 字符")
    print("=" * 100)

    print(
        plain_text[:5000]
    )


if __name__ == "__main__":
    main()
