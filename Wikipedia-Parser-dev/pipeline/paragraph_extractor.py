#!/usr/bin/env python3
"""按 section 文本提取段落。

段落之间用一个或多个空行分隔；单个换行会保留在段落内部，
避免把列表项、折行文本或其他 wikitext 格式提前拆开。

子标题（``=== Title ===`` 及更深层级）会更新段落标题路径，
同时从段落正文中移除；外层 section 标题始终是路径的第一个元素。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, List


@dataclass
class Paragraph:
    """表示 section 内的一个段落。"""

    section_no: int
    paragraph_no: int
    # 用冒号连接的标题路径，从所属 section 到任意层级子标题，
    # 例如 ``Section:Subsection:Topic``。
    toc: str | None
    text: str
    # `text` is replaced with the final visible text by the WTP pipeline; the
    # source remains available for storage and troubleshooting.
    raw_wikitext: str | None = None
    # The exact MWP-replaced input given to WTP and Wtp.expand() output before
    # ProjectA converts it to paragraph text.
    wtp_input: str | None = None
    expanded_wikitext: str | None = None
    parse_error: str | None = None


# 空行中允许出现空格或 Tab。前后查找用于避免把一个 CRLF
# 误判成两个独立换行。
_PARAGRAPH_SEPARATOR_RE = re.compile(
    r"(?:\r\n|(?<!\r)\n|(?<!\n)\r)[ \t]*"
    r"(?:(?:\r\n|(?<!\r)\n|(?<!\n)\r)[ \t]*)+"
)
_SUBSECTION_HEADER_RE = re.compile(
    r"^(?P<marks>={3,})[ \t]*(?P<title>[^=\r\n].*?)[ \t]*(?P=marks)[ \t]*$",
    re.MULTILINE,
)


def _update_heading_path(
    heading_path: list[str], heading_level: int, base_level: int, title: str
) -> list[str]:
    """进入嵌套标题后，返回更新后的标题路径。"""
    # section 标题默认是二级标题，因此三级标题保留 1 个路径节点，
    # 四级标题保留 2 个，以此类推。
    keep_count = max(0, heading_level - base_level)
    return heading_path[:keep_count] + [title]


def _split_block_by_subsections(
    block: str,
    heading_path: list[str],
    base_level: int,
) -> tuple[list[tuple[str, list[str]]], list[str]]:
    """移除子标题行，并把正文片段与对应的标题路径关联起来。"""
    matches = list(_SUBSECTION_HEADER_RE.finditer(block))
    if not matches:
        text = block.strip()
        return ([(text, heading_path)] if text else []), heading_path

    chunks: list[tuple[str, list[str]]] = []
    current_path = heading_path
    cursor = 0
    for match in matches:
        before = block[cursor : match.start()].strip()
        if before:
            chunks.append((before, current_path))

        title = match.group("title").strip()
        heading_level = len(match.group("marks"))
        current_path = _update_heading_path(
            current_path, heading_level, base_level, title
        )
        cursor = match.end()

    after = block[cursor:].strip()
    if after:
        chunks.append((after, current_path))
    return chunks, current_path


def extract_paragraphs(sections: list, page_id: Any) -> List[Paragraph]:
    """使用空行将每个 section 切分为段落。

    Args:
        sections: 来自 :mod:`section_extractor` 的 Section 对象列表。
        page_id: 页面 ID，仅为保持提取器接口一致而保留。

    Returns:
        按文档顺序返回段落列表。每个 section 内的 ``paragraph_no``
        从 1 开始，``toc`` 为用冒号连接的 section/子标题路径。
    """
    if not sections:
        return []

    paragraphs: List[Paragraph] = []
    for section in sections:
        text = section.text or section.raw_text
        if not text or not text.strip():
            continue

        paragraph_no = 0
        heading_path = [section.toc] if section.toc else []
        base_level = 2 if section.toc else 0
        for block in _PARAGRAPH_SEPARATOR_RE.split(text):
            block = block.strip()
            if not block:
                continue
            chunks, heading_path = _split_block_by_subsections(
                block, heading_path, base_level
            )
            for chunk, chunk_path in chunks:
                paragraph_no += 1
                paragraphs.append(
                    Paragraph(
                        section_no=section.section_no,
                        paragraph_no=paragraph_no,
                        toc=":".join(chunk_path) or None,
                        text=chunk,
                        raw_wikitext=chunk,
                    )
                )

    return paragraphs
