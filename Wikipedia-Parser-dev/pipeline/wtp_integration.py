"""针对单独安装的 ProjectB 包的狭小适配器。

worker 拥有自己的 Wtp 实例。它打开选定的版本化 SQLite DB
只读，并禁用 Wikidata HTTP 回退，因此缓存未命中不能阻塞
批次或更改数据资产。
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Mapping

import mwparserfromhell

_WTP: Any | None = None


def _resolve_path(value: str, base: Path) -> Path:
    path = Path(value)
    return (base / path).resolve() if not path.is_absolute() else path.resolve()


def load_wtp_settings(config: Mapping[str, Any], config_base: Path) -> dict[str, Any]:
    """从统一配置的 ``wtp`` 部分解析 WTP 设置。"""
    db_value = config.get("db_path")
    if not db_value:
        raise ValueError("缺少 wtp 配置键：db_path")

    db_path = _resolve_path(str(db_value), config_base)
    if not db_path.is_file():
        raise FileNotFoundError(f"配置的 WTP DB 不存在：{db_path}")
    return {
        "db_path": str(db_path),
        "lang_code": str(config.get("lang_code", "en")),
        "project": str(config.get("project", "wikipedia")),
        "expand_timeout": float(config.get("expand_timeout", 60.0)),
        # 这些默认值是为批次流水线刻意设置的。ProjectB
        # 仍保留其正常可写/在线模式用于独立使用。
        "read_only": bool(config.get("read_only", True)),
        "wikidata_offline": bool(config.get("wikidata_offline", True)),
    }


def initialize_wtp_worker(settings: Mapping[str, Any] | None) -> None:
    """处理池初始化器：永远不要跨 worker 共享 Wtp/Lua 状态。"""
    global _WTP
    if settings is None:
        _WTP = None
        return
    from wikitextprocessor import Wtp

    _WTP = Wtp(
        db_path=settings["db_path"],
        lang_code=settings["lang_code"],
        project=settings["project"],
        quiet=True,
        quiet_output=True,
        read_only=settings["read_only"],
        wikidata_offline=settings["wikidata_offline"],
    )


def begin_page(page_title: str) -> None:
    if _WTP is None:
        raise RuntimeError("WTP worker 未初始化")
    _WTP.start_page(page_title)


def analyze_wikitext(raw_wikitext: str) -> tuple[int, str | None]:
    """使用 MWP 验证 paragraph 结构而不进行不必要的树遍历。

    ``build_bundle`` 只需要解析失败信号。使用 ``filter_templates(recursive=True)``
    计算模板会再次遍历完整树，但结果从未被消费。
    """
    try:
        mwparserfromhell.parse(raw_wikitext)
        return 0, None
    except Exception as exc:  # MWP 绝不能阻止 paragraph/批次。
        return 0, f"MWP {type(exc).__name__}: {exc}"


def expand_to_text(wikitext: str, timeout: float) -> tuple[str, str, str | None]:
    """扩展 Wikitext 并返回最终文本、原始扩展和任何错误。

    保留原始扩展是因为它是 WTP/Lua 处理与 ProjectA 可见文本转换之间的
    诊断边界。
    """
    if _WTP is None:
        raise RuntimeError("WTP worker 未初始化")
    before = len(_WTP.errors)
    try:
        expanded = _WTP.expand(wikitext, timeout=timeout)
        errors = _WTP.errors[before:]
        error = "; ".join(str(item) for item in errors) or None
        return expanded_wikitext_to_text(expanded), expanded, error
    except Exception as exc:  # Lua/模板失败按 paragraph 隔离。
        # 没有成功的 WTP 输出；记录精确输入作为
        # 可恢复的中间值并使错误明确。
        return (
            expanded_wikitext_to_text(wikitext),
            wikitext,
            f"WTP {type(exc).__name__}: {exc}",
        )


def _remove_file_links(text: str) -> str:
    pattern = re.compile(r"\[\[\s*(?:File|Image):", flags=re.I)
    while (match := pattern.search(text)) is not None:
        start, pos, depth = match.start(), match.end(), 1
        while pos < len(text) - 1:
            pair = text[pos : pos + 2]
            if pair == "[[":
                depth, pos = depth + 1, pos + 2
            elif pair == "]]":
                depth, pos = depth - 1, pos + 2
                if depth == 0:
                    break
            else:
                pos += 1
        if depth:
            break
        text = text[:start] + text[pos:]
    return text


class _VisibleTextParser(HTMLParser):
    block_tags = frozenset(
        {
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
    )
    skip_tags = frozenset({"style", "script", "ref", "references", "templatestyles"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        classes = dict(attrs).get("class", "") or ""
        if tag in self.skip_tags or "error" in classes.casefold().split():
            self.skip_stack.append(tag)
        elif not self.skip_stack:
            self.parts.append("\n" if tag in self.block_tags or tag == "br" else "")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if self.skip_stack:
            if tag == self.skip_stack[-1]:
                self.skip_stack.pop()
        elif tag in self.block_tags:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip_stack:
            self.parts.append(data)


def expanded_wikitext_to_text(expanded: str) -> str:
    """将 WTP 扩展后的 wikitext 转换为 paragraph 文本。

    普通 ``[[...]]`` 链接在这里故意保持完整。sentence
    pipeline 消费这个 paragraph 文本并在那里应用其建立的
    ``_process_wikilinks`` 表示。类别和文件/图片链接
    仍然被移除，因为它们不是 sentence 内容。
    """
    text = re.sub(r"<!--.*?-->", "", expanded, flags=re.S)
    text = re.sub(r"<ref\b[^>]*>.*?</ref\s*>", "", text, flags=re.I | re.S)
    text = re.sub(r"<ref\b[^>]*/\s*>", "", text, flags=re.I)
    text = _remove_file_links(text)
    text = re.sub(r"\[\[\s*:?\s*Category:[^\[\]]*\]\]", "", text, flags=re.I)
    text = re.sub(r"(?m)^\s*=+\s*(.*?)\s*=+\s*$", r"\1", text)
    text = re.sub(r"\[(?:https?:)?//[^\s\]]+\s+([^\]]+)\]", r"\1", text, flags=re.I)
    text = re.sub(r"\[https?://[^\]]+\]", "", text, flags=re.I)
    text = text.replace("'''", "").replace("''", "")
    text = re.sub(r"__[A-Z][A-Z0-9_]*__", "", text)
    parser = _VisibleTextParser()
    try:
        parser.feed(text)
        parser.close()
        text = "".join(parser.parts)
    except Exception:
        text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"(?m)^\s*\{\|.*$|^\s*\|\}\s*$|^\s*\|-\s*.*$", "", text)
    text = re.sub(r"[ \t]+", " ", text.replace("\xa0", " "))
    text = "\n".join(line.strip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()
