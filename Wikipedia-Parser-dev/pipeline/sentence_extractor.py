#!/usr/bin/env python3
"""使用 sentencex 将每个段落切分为句子。

每个句子包含：
  * ``section_no``：所属的 section
  * ``paragraph_no``：该句所在的段落编号
  * ``sentence_no``：该句在 section 内的顺序编号，从 1 开始
  * ``toc``：继承自段落的标题路径
  * ``raw_text``：分句前清理 ref 占位符后的句子文本
  * ``text``：进一步处理后的句子文本（普通 wikilink 会变成 ``[[target↓target]]``）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, List


# ``component_extractor`` 会有意保留普通 wikilink。
# 句子级消费者希望把普通链接表示成原括号内“目标↓目标”的形式。
# 目前只转换不带显示文本分隔符的 ``[[Target]]``；带 ``|`` 的链接按规则单独处理。
_WIKILINK_RE = re.compile(r"\[\[([^\[\]]+)\]\]")
_REF_PLACEHOLDER_RE = re.compile(
    r"♣  ♣  ♣  ref-[^♣\r\n]+?♣  ♣  ♣", re.IGNORECASE
)
# 列表标记正则: 匹配以 * # ; : 开头的行。列表不会作为组件提取，
# 因而这些行首标记会保留到分句阶段；MULTILINE 让 ^ 匹配段落内的每一行开头。
_LIST_MARKER_RE = re.compile(r"^[*#;:]", re.MULTILINE)
_LIST_LINE_RE = re.compile(
    r"^[ \t]*(?P<markers>[*#;:]+)[ \t]*(?P<content>.*?)[ \t]*$"
)
_LIST_ITEM_END_RE = re.compile(r"[ \t]*[.;\uFF1B\u3002]+[ \t]*$")


def _format_wikilink(match: re.Match[str]) -> str:
    """把单个普通 ``[[Target]]`` 链接转换成 ``[[Target↓Target]]``。

    对于只包含一个显示文本分隔符的 ``[[XXX1|XXX2]]``，结果会变成
    ``[[XXX2↓XXX1]]``，也就是“显示文本在前，链接目标在后”。
    如果包含多个竖线（如 ``[[A|B|C]]``），则保持不变。
    """
    target = match.group(1)
    if "|" in target:
        parts = target.split("|")
        if len(parts) == 2:
            link_target = parts[0].strip()
            display = parts[1].strip()
            if link_target and display:
                return f"[[{display}\u2193{link_target}]]"
        # 出现多个竖线，或任一部分为空时，保持原样
        return match.group(0)
    target = target.strip()
    return f"[[{target}\u2193{target}]]"


def _process_wikilinks(text: str) -> str:
    """把普通 wikilink 标记替换成 ``[[target↓target]]`` 形式。"""
    return _WIKILINK_RE.sub(_format_wikilink, text)


def _remove_ref_placeholders(text: str) -> str:
    """在分句前移除已提取出来的参考文献占位符。"""
    return _REF_PLACEHOLDER_RE.sub("", text)


def _remove_list_markers(text: str) -> str:
    """在分句前移除 wikitext 列表标记（行首的 ``* # ; :``）。"""
    return _LIST_MARKER_RE.sub("", text)


@dataclass
class _ListItem:
    """表示一个 wikitext 列表项及其嵌套子项。"""

    depth: int
    content: str
    children: list["_ListItem"]


def _clean_list_item_content(content: str) -> str:
    """移除列表项结尾多余的标点，交由结构化渲染统一补齐。"""
    return _LIST_ITEM_END_RE.sub("", content.strip()).strip()


def _render_list_item(item: _ListItem) -> str:
    """递归渲染单个列表项；若存在子项，则用冒号引出。"""
    content = _clean_list_item_content(item.content)
    rendered_children = [
        (child, rendered)
        for child in item.children
        if (rendered := _render_list_item(child))
    ]
    if not rendered_children:
        return content

    # 嵌套同级项会紧凑拼接。普通同级项之间使用逗号；
    # 如果前后某项带有子树，则用分号分隔，保留层级边界。
    child_node, child_text = rendered_children[0]
    for next_node, child in rendered_children[1:]:
        separator = ";" if child_node.children or next_node.children else ","
        child_text += f"{separator}{child}"
        child_node = next_node
    if not content:
        return child_text

    # 父项通过一个标准 ASCII 冒号引出其子项。
    content = content.rstrip(" \t:：;；")
    return f"{content}: ({child_text})"


def _render_list_block(items: list[tuple[int, str]]) -> str:
    """把一整块列表折叠成一个具有层级结构的句子主体。

    顶层列表项之间用分号分隔；父项和子项之间用冒号连接，子项整体包在括号里。
    调用方会额外补上句号，从而保护整块列表不被 sentence segmentation 拆开。
    """
    roots: list[_ListItem] = []
    stack: list[_ListItem] = []

    for depth, content in items:
        node = _ListItem(depth=depth, content=content, children=[])
        while stack and stack[-1].depth >= depth:
            stack.pop()
        if stack:
            stack[-1].children.append(node)
        else:
            roots.append(node)
        stack.append(node)

    return "; ".join(
        rendered for root in roots if (rendered := _render_list_item(root))
    )


def _prepare_lists_for_segmentation(text: str) -> tuple[str, dict[str, str]]:
    """在调用 ``sentencex`` 前，用不透明 token 替换整块列表。

    如果前一行以冒号结尾，则会让该行文本与列表 token 保持在同一句中；
    否则列表会被当作独立的一句。token 可以避免列表项内部的标点额外触发分句。
    """
    lines = text.splitlines()
    output_lines: list[str] = []
    replacements: dict[str, str] = {}
    token_prefix = "WIKILISTBLOCKTOKEN"
    while token_prefix in text:
        token_prefix = f"X{token_prefix}"

    index = 0
    while index < len(lines):
        match = _LIST_LINE_RE.fullmatch(lines[index])
        if match is None:
            output_lines.append(lines[index])
            index += 1
            continue

        items: list[tuple[int, str]] = []
        while index < len(lines):
            match = _LIST_LINE_RE.fullmatch(lines[index])
            if match is None:
                break
            items.append((len(match.group("markers")), match.group("content")))
            index += 1

        rendered = _render_list_block(items)
        if not rendered:
            continue

        token = f"{token_prefix}{len(replacements)}END"
        replacements[token] = rendered
        protected_list = f"{token}."

        # 冒号通常意味着后面显式引出一个列表。
        # 把 token 接到这一行后面，可以让 sentencex 把引导语和整个列表保留在同一句中。
        if output_lines and output_lines[-1].rstrip().endswith((":", "：")):
            output_lines[-1] = f"{output_lines[-1].rstrip()} {protected_list}"
        else:
            output_lines.append(protected_list)

    return "\n".join(output_lines), replacements


def _restore_list_tokens(text: str, replacements: dict[str, str]) -> str:
    """在分句完成后恢复之前替换掉的列表正文。"""
    for token, rendered in replacements.items():
        text = text.replace(token, rendered)
    return text


def _collapse_list_blocks(text: str) -> str:
    """为不需要分句保护的调用方直接折叠列表块。"""
    prepared, replacements = _prepare_lists_for_segmentation(text)
    return _restore_list_tokens(prepared, replacements)


def _is_list_only_text(text: str) -> bool:
    """判断一个段落是否只包含非空的列表项行。"""
    lines = [line for line in text.splitlines() if line.strip()]
    return bool(lines) and all(_LIST_LINE_RE.fullmatch(line) for line in lines)


@dataclass
class Sentence:
    """表示段落中的一个句子。"""

    section_no: int
    sentence_no: int
    toc: str | None
    raw_text: str
    text: str
    paragraph_no: int = 1


def extract_sentences(
    paragraphs: list,
    page_id: Any,
    lang: str = "en",
    skip_tocs: set[str] | None = None,
) -> List[Sentence]:
    """使用 sentencex 把段落切分为句子。

    Args:
        paragraphs: 来自 paragraph_extractor 的 Paragraph 对象列表。
        page_id: 页面 ID，用于日志或调试。
        lang: sentencex 使用的语言代码，默认 ``"en"``。
        skip_tocs: 一组小写 section 标题；命中的段落不会被分句，
            而是整段作为一句保留，且不做 wikilink 处理。

    Returns:
        返回 Sentence 对象列表，其中包含：
          - ``section_no``：所属 section
          - ``paragraph_no``：section 内段落编号
          - ``sentence_no``：section 内句子编号，从 1 开始
          - ``toc``：继承自段落的标题路径
          - ``raw_text``：分句前清理 ref 后得到的句子文本
          - ``text``：进一步处理后的句子文本
    """
    if not paragraphs:
        return []

    # 延迟导入 sentencex（避免模块级别导入问题）
    import sentencex

    all_sentences: List[Sentence] = []
    section_sentence_nos: dict[int, int] = {}

    # 段落提取通常会按空行拆块，但列表有时会和前面的冒号引导语被空行隔开；
    # 因此这里会在分句前把这两部分重新合并。
    paragraph_entries: list[tuple[Any, str]] = []
    for paragraph in paragraphs:
        # 为兼容旧接口，仍然接受 Section 对象：
        # 此时整个 section 会被视为 paragraph 1。
        if hasattr(paragraph, "paragraph_no"):
            text = paragraph.text or ""
        else:
            text = paragraph.text or paragraph.raw_text or ""
        text = _remove_ref_placeholders(text)
        if paragraph_entries:
            previous_paragraph, previous_text = paragraph_entries[-1]
            same_section = previous_paragraph.section_no == paragraph.section_no
            previous_ends_colon = previous_text.rstrip().endswith((":", "："))
            if same_section and previous_ends_colon and _is_list_only_text(text):
                paragraph_entries[-1] = (
                    previous_paragraph,
                    f"{previous_text.rstrip()}\n{text.strip()}",
                )
                continue
        paragraph_entries.append((paragraph, text))

    for paragraph, text in paragraph_entries:
        if hasattr(paragraph, "paragraph_no"):
            paragraph_no = paragraph.paragraph_no
            toc_value = paragraph.toc
        else:
            paragraph_no = 1
            toc_value = paragraph.toc
        # 在分句前先折叠列表层级。不透明 token 可以保护整个列表
        # 以及前面的冒号引导语，使其即便包含句末标点也不会被拆散。
        text, list_replacements = _prepare_lists_for_segmentation(text)
        if not text or not text.strip():
            continue

        # 特殊节 (参考文献/外部链接等): 不分句，整段作为一句，不处理 wikilinks
        toc_key = (
            toc_value.strip().lower()
            if isinstance(toc_value, str) else ""
        )
        skip_segmentation = skip_tocs is not None and any(
            toc_key == skip_toc or toc_key.startswith(f"{skip_toc}:")
            for skip_toc in skip_tocs
        )

        if skip_segmentation:
            segments = [text]
        else:
            try:
                segments = sentencex.segment(lang, text)
            except Exception:
                # 兜底策略：把整个段落当成一句
                segments = [text]

        sentence_no = section_sentence_nos.get(paragraph.section_no, 0)
        paragraph_sentence_count = 0
        for seg in segments:
            seg = _restore_list_tokens(seg, list_replacements).strip()
            if not seg:
                continue
            sentence_no += 1
            paragraph_sentence_count += 1
            # 特殊节不分句且不处理 wikilinks，text 保持原样
            seg_text = seg if skip_segmentation else _process_wikilinks(seg)
            all_sentences.append(
                Sentence(
                    section_no=paragraph.section_no,
                    sentence_no=sentence_no,
                    toc=toc_value,
                    raw_text=seg,
                    text=seg_text,
                    paragraph_no=paragraph_no,
                )
            )

        # 如果没有成功提取出句子，则把整个段落补成一句。
        if paragraph_sentence_count == 0 and text.strip():
            sentence_no += 1
            raw = _restore_list_tokens(text, list_replacements).strip()
            seg_text = raw if skip_segmentation else _process_wikilinks(raw)
            all_sentences.append(
                Sentence(
                    section_no=paragraph.section_no,
                    sentence_no=sentence_no,
                    toc=toc_value,
                    raw_text=raw,
                    text=seg_text,
                    paragraph_no=paragraph_no,
                )
            )

        section_sentence_nos[paragraph.section_no] = sentence_no

    return all_sentences
