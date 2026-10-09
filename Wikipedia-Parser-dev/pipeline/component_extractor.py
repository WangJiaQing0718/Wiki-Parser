"""提取维基文本组件，并将其替换为稳定的占位符。"""

from __future__ import annotations

import re
from typing import Any, Sequence

import mwparserfromhell
from mwparserfromhell.nodes import Comment, ExternalLink, Tag, Template, Text, Wikilink

# 组件类型；每种类型分别写入 component_<type> 表。
COMPONENT_TYPES = (
    "infobox", "table", "independent_template", "wikilinks", "external_links",
    "file", "image", "ref",
)

# 列表块刻意排除在组件提取之外，在 WTP 阶段保持原样，后续交由 sentence_extractor 处理。
_LIST_MARKER_RE = re.compile(r"^[ \t]*[*#;:]")
_COMPONENT_PLACEHOLDER_RE = re.compile(r"♣  ♣  ♣  ([^♣]+)♣  ♣  ♣")

# 遵循通用 start/top/begin 和 end/bottom 后缀约定的成对模板名称
_TEMPLATE_BLOCK_OPENERS = ("start", "top", "begin")
_TEMPLATE_BLOCK_CLOSERS = ("end", "bottom")

# 不遵循通用后缀约定的成对模板名称配对
_TEMPLATE_BLOCK_NAME_PAIRS = (
    # ("div col", "div col end"), 仅结构示例，这个应该不用排除
)


def _component_id(ctype: str, page_id: Any, seq: int) -> str:
    """根据组件类型、页面 ID 和序号生成唯一组件 ID。"""
    return f"{ctype}-{page_id}-{seq:04d}"


def restore_component_sources(
    wikitext: str, component_sources: dict[str, str]
) -> str:
    """将占位符还原为对应组件在源 wikitext 中的文本。"""
    return _COMPONENT_PLACEHOLDER_RE.sub(
        lambda match: component_sources.get(match.group(1), match.group(0)),
        wikitext,
    )


def _protect_list_blocks(wikitext: str) -> tuple[str, dict[str, str]]:
    """将连续的列表行暂时替换为令牌，并返回令牌到原文的映射。"""
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


def _template_block_marker(template: Template) -> tuple[str, str] | None:
    """检测一个模板节点是否是成对模板"""
    name = re.sub(r"[\s/_-]+", "", str(template.name).casefold())
    for opener, closer in _TEMPLATE_BLOCK_NAME_PAIRS:
        normalized_opener = re.sub(r"[\s/_-]+", "", opener.casefold())
        normalized_closer = re.sub(r"[\s/_-]+", "", closer.casefold())
        if name == normalized_opener:
            return "open", normalized_opener
        if name == normalized_closer:
            return "close", normalized_opener
    for suffix in _TEMPLATE_BLOCK_OPENERS:
        if name.endswith(suffix):
            return "open", name[:-len(suffix)]
    for suffix in _TEMPLATE_BLOCK_CLOSERS:
        if name.endswith(suffix):
            return "close", name[:-len(suffix)]
    return None


def _outermost_template_block_ranges(nodes: Sequence[Any]) -> dict[int, int]:
    """成对模板区块标记，返回最外层起止节点的索引映射。"""
    stack: list[tuple[int, str]] = []
    pairs: list[tuple[int, int]] = []
    for index, node in enumerate(nodes):
        if not isinstance(node, Template):
            continue
        marker = _template_block_marker(node)
        if marker is None:
            continue
        direction, family = marker
        if direction == "open":
            stack.append((index, family))
            continue
        if not stack:
            continue
        open_index, open_family = stack[-1]
        if family and family != open_family:
            continue
        stack.pop()
        pairs.append((open_index, index))

    outermost: dict[int, int] = {}
    for start, end in sorted(pairs, key=lambda pair: (pair[0], -pair[1])):
        if not any(outer_start < start and end < outer_end
                   for outer_start, outer_end in outermost.items()):
            outermost[start] = end
    return outermost

# 注意：下面两个函数是互补覆盖情况，缺一不可。
# 若后续按行处理逻辑出现删除有用文本的情况时，可以考虑移除按行判断的逻辑，但必须补上 simplewiki中 page_id:302 首段这类段落中包含正文的补丁。
# 存在段落只有模板，但模板节点本身含换行，单纯按行判断不准确。
def _template_only_paragraph_node_indexes(nodes: Sequence[Any]) -> set[int]:
    """找出仅由模板组成的段落中的模板节点索引"""
    result: set[int] = set()
    paragraph_templates: list[int] = []
    has_non_template_content = False

    def finish_paragraph() -> None:
        """结束当前段落，并记录其中不含其他内容的模板节点。"""
        nonlocal paragraph_templates, has_non_template_content
        if paragraph_templates and not has_non_template_content:
            result.update(paragraph_templates)
        paragraph_templates = []
        has_non_template_content = False

    for index, node in enumerate(nodes):
        if isinstance(node, Template):
            paragraph_templates.append(index)
            continue
        if isinstance(node, Text):
            text = str(node)
            position = 0
            for separator in re.finditer(r"\r?\n[ \t]*\r?\n", text):
                if text[position:separator.start()].strip():
                    has_non_template_content = True
                finish_paragraph()
                position = separator.end()
            if text[position:].strip():
                has_non_template_content = True
            continue
        if str(node).strip():
            has_non_template_content = True
    finish_paragraph()
    return result

# 段落中包含正文，但是仍然存在可过滤的模板行，单纯按段落判断不准确
def _template_only_line_node_indexes(source: str, nodes: Sequence[Any]) -> set[int]:
    """找出仅由模板组成的行中的模板节点索引"""
    result: set[int] = set()
    offsets: list[tuple[int, int, int]] = []
    offset = 0
    for index, node in enumerate(nodes):
        raw_node = str(node)
        start = offset
        offset += len(raw_node)
        if isinstance(node, Template) and "\n" not in raw_node and "\r" not in raw_node:
            offsets.append((index, start, offset))

    checked_lines: set[tuple[int, int]] = set()
    for _index, start, end in offsets:
        line_start = source.rfind("\n", 0, start) + 1
        line_end = source.find("\n", end)
        if line_end < 0:
            line_end = len(source)
        line_range = (line_start, line_end)
        if line_range in checked_lines:
            continue
        checked_lines.add(line_range)

        line_nodes = mwparserfromhell.parse(source[line_start:line_end]).nodes
        if not any(isinstance(node, Template) for node in line_nodes):
            continue
        if not all(
            isinstance(node, Template)
            or (isinstance(node, Text) and not str(node).strip())
            for node in line_nodes
        ):
            continue
        result.update(
            node_index
            for node_index, node_start, node_end in offsets
            if line_start <= node_start and node_end <= line_end
        )
    return result


def extract_and_templatize(
    wikitext: str | None,
    page_id: Any,
    comps: dict[str, list[tuple[str, str]]] | None = None,
    component_sources: dict[str, str] | None = None,
) -> tuple[str, dict[str, list[tuple[str, str]]]]:
    """提取组件并替换为占位符；可选记录占位符对应的原始文本。"""
    if comps is None:
        comps = {t: [] for t in COMPONENT_TYPES}

    def take(ctype: str, text: str, source_text: str | None = None) -> str:
        """登记一个组件并返回其占位符。"""
        cid = _component_id(ctype, page_id, len(comps[ctype]) + 1)
        comps[ctype].append((cid, text))
        if component_sources is not None:
            component_sources[cid] = text if source_text is None else source_text
        return f"♣  ♣  ♣  {cid}♣  ♣  ♣"

    # 阶段一：提取成对模板和独立模板行，并扫描顶层节点；内部内容由外层节点整体收录。
    # 独立模板的起止标记所在物理行只能包含该模板和空白字符。
    source = wikitext or ""
    code = mwparserfromhell.parse(source)
    for comment in code.filter(recursive=True, forcetype=Comment):
        code.remove(comment)
    source = str(code)
    nodes = list(code.nodes)
    block_ranges = _outermost_template_block_ranges(nodes)
    template_only_paragraph_nodes = _template_only_paragraph_node_indexes(nodes)
    template_only_line_nodes = _template_only_line_node_indexes(source, nodes)
    parts: list[str] = []
    offset = 0
    node_index = 0
    while node_index < len(nodes):
        node = nodes[node_index]
        raw_node = str(node)
        node_start = offset
        node_end = node_start + len(raw_node)
        # 判断node是否属于成对模板块的起始节点
        block_end_index = block_ranges.get(node_index)
        if block_end_index is not None:
            block_end = node_end
            for inner_index in range(node_index + 1, block_end_index + 1):
                block_end += len(str(nodes[inner_index]))
            parts.append(take("independent_template", source[node_start:block_end]))
            offset = block_end
            node_index = block_end_index + 1
            continue
        offset = node_end
        # 判断node是否属于独立模板行的节点
        if isinstance(node, Template) and str(node.name).strip().lower().startswith("infobox"):
            parts.append(take("infobox", raw_node))
        elif isinstance(node, Template):
            marker = _template_block_marker(node)
            line_start = source.rfind("\n", 0, node_start) + 1
            line_end = source.find("\n", node_end)
            if line_end < 0:
                line_end = len(source)
            if (
                marker is None
                and
                (
                    (
                        not source[line_start:node_start].strip()
                        and not source[node_end:line_end].strip()
                    )
                    or node_index in template_only_line_nodes
                    or node_index in template_only_paragraph_nodes
                )
            ):
                parts.append(take("independent_template", raw_node))
            else:
                parts.append(raw_node)
        elif isinstance(node, Tag) and str(node.tag).strip().lower() == "table":
            parts.append(take("table", raw_node))
        else:
            parts.append(raw_node)
        node_index += 1

    # 阶段二：提取其余行内组件。此阶段临时将列表替换为不可解析的标记，并在返回前还原。
    # 重新解析文本；用 ♣ 分隔的占位符会作为普通文本保留。<ref> 或 File:/Image: 链接会整体收录其内部内容，因为它们各自都是一个顶层节点。
    #   * <ref>...</ref>      -> 写入 ref 表，并替换为占位符
    #   * [[File:...]]        -> 写入 file 表，并替换为占位符
    #   * [[Image:...]]       -> 写入 image 表，并替换为占位符
    #   * 其他 [[...]]        -> 写入 wikilinks 表，在 text_process 中保留原文
    #   * [url ...] / 裸 URL  -> 写入 external_links 表，并替换为占位符
    inline_source, list_blocks = _protect_list_blocks("".join(parts))
    code2 = mwparserfromhell.parse(inline_source)
    parts2: list[str] = []
    for node in code2.nodes:
        if isinstance(node, Comment):
            continue
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
                parts2.append(str(node))  # 在 text_process 中保留原始 [[...]]
        elif isinstance(node, ExternalLink):
            parts2.append(
                take(
                    "external_links",
                    str(node.url).strip(),
                    source_text=str(node),
                )
            )
        else:
            parts2.append(str(node))

    # 仅保留组件提取产生的占位符，不额外调整换行或首尾空白。
    text = "".join(parts2)
    for token, raw_list in list_blocks.items():
        text = text.replace(token, raw_list)

    return text, comps
