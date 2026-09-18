"""ProcessA's narrow adapter around the separately-installed ProjectB package.

The worker owns one Wtp instance.  It opens the selected versioned SQLite DB
read-only and disables Wikidata HTTP fallback, so a cache miss cannot stall a
batch or change the data asset.
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


def load_wtp_settings(
    config: Mapping[str, Any], config_base: Path
) -> dict[str, Any]:
    """Resolve WTP settings from the ``wtp`` section of the unified config."""
    db_value = config.get("db_path")
    if not db_value:
        raise ValueError("Missing wtp config key: db_path")

    db_path = _resolve_path(str(db_value), config_base)
    if not db_path.is_file():
        raise FileNotFoundError(f"Configured WTP DB does not exist: {db_path}")
    return {
        "db_path": str(db_path),
        "lang_code": str(config.get("lang_code", "en")),
        "project": str(config.get("project", "wikipedia")),
        "expand_timeout": float(config.get("expand_timeout", 15.0)),
        # These defaults are deliberate for the batch pipeline.  ProjectB
        # remains capable of its normal writable/online mode for standalone use.
        "read_only": bool(config.get("read_only", True)),
        "wikidata_offline": bool(config.get("wikidata_offline", True)),
    }


def initialize_wtp_worker(settings: Mapping[str, Any] | None) -> None:
    """Process-pool initializer: never share Wtp/Lua state across workers."""
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
        raise RuntimeError("WTP worker was not initialized")
    _WTP.start_page(page_title)


def analyze_wikitext(raw_wikitext: str) -> tuple[int, str | None]:
    """Validate paragraph structure with MWP without an unused tree walk.

    ``build_bundle`` only needs the parse failure signal.  Counting templates
    with ``filter_templates(recursive=True)`` traversed the full tree once more
    for every paragraph, but its result was never consumed.
    """
    try:
        mwparserfromhell.parse(raw_wikitext)
        return 0, None
    except Exception as exc:  # MWP must not stop the paragraph/batch.
        return 0, f"MWP {type(exc).__name__}: {exc}"


def expand_to_text(wikitext: str, timeout: float) -> tuple[str, str, str | None]:
    """Expand Wikitext and return final text, raw expansion, and any error.

    The raw expansion is retained separately because it is the diagnostic
    boundary between WTP/Lua processing and ProjectA's visible-text conversion.
    """
    if _WTP is None:
        raise RuntimeError("WTP worker was not initialized")
    before = len(_WTP.errors)
    try:
        expanded = _WTP.expand(wikitext, timeout=timeout)
        errors = _WTP.errors[before:]
        error = "; ".join(str(item) for item in errors) or None
        return expanded_wikitext_to_text(expanded), expanded, error
    except Exception as exc:  # Lua/template failures are isolated per paragraph.
        # No successful WTP output exists; record the exact input as the
        # recoverable intermediate value and make the error explicit.
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
            pair = text[pos:pos + 2]
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
    block_tags = frozenset({
        "p", "div", "table", "tr", "td", "th", "ul", "ol", "li", "dl",
        "dt", "dd", "section", "header", "footer", "h1", "h2", "h3", "h4",
        "h5", "h6",
    })
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
    """Convert WTP-expanded wikitext to paragraph text.

    Ordinary ``[[...]]`` links deliberately remain intact here.  The sentence
    pipeline consumes this paragraph text and applies its established
    ``_process_wikilinks`` representation there.  Categories and file/image
    links are still removed because they are not sentence content.
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
