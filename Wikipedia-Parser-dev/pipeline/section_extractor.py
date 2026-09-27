#!/usr/bin/env python3
"""按 ``== Title ==`` 标记把 Wikipedia 文章切分成 section。

切分后的每个 section 包含：
  * ``section_no``：从 1 开始的 section 编号
  * ``toc``：section 标题；首个 ``==`` 之前的内容标题为 ``Summary``
  * ``raw_text``：该 section 的原始 wikitext
  * ``text``：处理后的文本，目前与 ``raw_text`` 相同

切分规则：
  * 只按 ``==``（两个等号）切分，不按 ``===`` 或更深层级切分
  * 第一个 ``==`` 之前的内容属于 ``section_no = 1``（``toc = "Summary"``）
  * 此后的每个 ``==`` 都会开启一个新的 section，标题取自两侧等号之间的文本
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, List


@dataclass
class Section:
    """表示 Wikipedia 文章中的一个 section。"""

    section_no: int
    toc: str | None
    raw_text: str
    text: str
    # 原始 ``== Title ==`` 标题行（不含行尾换行）；Summary 等无标题节为 None。
    # 标题行本身不进 raw_text/text，由调用方（engine 拼 text_process）按需补回。
    heading: str | None = None


# 匹配 section 标题：``== Title ==``（两侧都必须恰好两个等号）
# 不匹配 ``=== Title ===``（三个及以上等号）
_SECTION_HEADER_RE = re.compile(r"^(={2})([^=].*[^=])\1\s*$", re.MULTILINE)


def extract_sections(wikitext: str | None, page_id: Any) -> List[Section]:
    """按 ``==`` 标记将 wikitext 切分为多个 section。

    Args:
        wikitext: 原始 wikitext 内容，可以为 ``None``。
        page_id: 页面 ID，用于日志或调试，解析本身并不依赖它。

    Returns:
        按文档顺序返回 Section 对象列表，其中：
          - ``section_no`` 从 1 开始
          - ``toc`` 为 section 标题，首段默认是 ``"Summary"``
          - ``raw_text`` 为该 section 的原始 wikitext
          - ``text`` 当前与 ``raw_text`` 相同，后续可继续处理
    """
    if not wikitext:
        return []

    sections: List[Section] = []

    # 找到所有 section 标题的位置
    matches = list(_SECTION_HEADER_RE.finditer(wikitext))

    if not matches:
        # 如果没有 section 标题，则整篇文章视为一个 section
        sections.append(
            Section(
                section_no=1,
                toc="Summary",
                raw_text=wikitext.strip(),
                text=wikitext.strip(),
            )
        )
        return sections

    # 第一个 ``==`` 之前的内容属于 section 1
    first_match = matches[0]
    if first_match.start() > 0:
        pre_content = wikitext[: first_match.start()].strip()
        if pre_content:
            sections.append(
                Section(
                    section_no=1,
                    toc="Summary",
                    raw_text=pre_content,
                    text=pre_content,
                )
            )

    # 逐个处理 section 标题
    for i, match in enumerate(matches):
        # 提取 section 标题（即 ``==`` 之间的文本）
        toc = match.group(2).strip()
        # 原始标题行。\s*$ 的回溯可能把行尾换行吞进 match，rstrip 还原成单行。
        heading = match.group(0).rstrip()

        # 确定该 section 的正文范围：
        # 从当前标题结束处，到下一个标题开始处（或文本末尾）
        section_start = match.end()
        if i + 1 < len(matches):
            section_end = matches[i + 1].start()
        else:
            section_end = len(wikitext)

        section_content = wikitext[section_start:section_end].strip()

        # section 编号按当前已有 sections 数量递增
        section_no = len(sections) + 1

        sections.append(
            Section(
                section_no=section_no,
                toc=toc,
                raw_text=section_content,
                text=section_content,
                heading=heading,
            )
        )

    return sections
