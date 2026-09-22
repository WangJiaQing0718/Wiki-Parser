# wikitextprocessor

这是一个用于处理 [WikiMedia 转储文件](https://dumps.wikimedia.org) 的 Python 包，适用于 [Wiktionary](https://www.wiktionary.org)、[Wikipedia](https://www.wikipedia.org) 等，可用于数据提取、错误检查、离线转换为 HTML 或其他格式，以及其他用途。主要功能包括：

* 解析转储文件，包括内置支持并行处理页面
* [Wikitext](https://en.wikipedia.org/wiki/Help:Wikitext) 语法解析器，将整个页面转换为解析树
* 从转储文件中提取模板定义和 [Scribunto](https://www.mediawiki.org/wiki/Extension:Scribunto/Lua_reference_manual) Lua 模块定义
* 扩展选定模板或所有模板，并启发式识别需要在解析前扩展的模板（例如，发出表开始和结束标签的模板）
* 处理并扩展 Wikitext 解析函数
* 处理、执行并扩展 Scribunto Lua 模块（它们在 Wiktionary 等中被广泛使用，例如用于生成许多语言的 [IPA](https://en.wikipedia.org/wiki/International_Phonetic_Alphabet) 字符串）
* 为在解析前解析整体页面结构但随后扩展页面某些部分模板的应用程序，提供页面的可控部分扩展
* 在扩展模板时捕获模板参数的信息，因为模板参数通常包含扩展内容中不可用的有用信息。

此模块主要设计为处理 Wiktionary 或 Wikipedia 数据的其他包的构建块，特别是用于数据提取。您需要编写代码来使用它。

对于使用此包进行预提取的数据模块，请参见：

* [Wiktextract](https://github.com/tatuylonen/wiktextract/) 用于从 Wiktionary 提取丰富的机器可读词典。您还可以在 [kaikki.org](https://kaikki.org/dictionary) 找到预提取的机器可读 Wiktionary 数据，格式为 JSON。

## 快速开始

### 安装

从源代码安装：

```
git clone --recurse-submodules --shallow-submodules https://github.com/tatuylonen/wikitextprocessor.git
cd wikitextprocessor
python -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e .
```

### 运行测试

此包包含使用 `unittest` 框架编写的测试。
测试依赖项可以通过命令 `python -m pip install -e .[dev]` 安装。

要在顶层目录运行测试，请使用以下命令：

```
make test
```

要运行特定测试，请使用以下语法：

```
python -m unittest tests.test_[模块].[模块]Tests.test_[名称]
```

Python 的 unittest 框架的帮助和选项可以通过以下方式访问：

```
python -m unittest -h
```

### 获取 WikiMedia 转储文件

此包主要用于处理 Wiktionary 和 Wikipedia 转储文件（尽管您也可以用它处理以 Wikitext 格式存在的单个页面或其他文件）。要下载 WikiMedia 转储文件，请前往 [转储下载页面](https://dumps.wikimedia.org/backup-index.html)。我们建议使用 `&lt;name&gt;-&lt;date&gt;-pages-articles.xml.bz2` 文件。

## API 文档

使用示例：

```python
from functools import partial
from typing import Any

from wikitextprocessor import Wtp, WikiNode, NodeKind, Page
from wikitextprocessor.dumpparser import process_dump


def page_handler(wtp: Wtp, page: Page) -> Any:
    wtp.start_page(page.title)
    # 处理解析树
    tree = wtp.parse(page.body)
    # 或获取扩展的纯文本
    text = wtp.expand(page.body)


wtp = Wtp(db_path="en_20230801.db", lang_code="en", project="wiktionary")

# 提取转储文件然后将页面保存到 SQLite 文件
process_dump(
    wtp,
    "enwiktionary-20230801-pages-articles.xml.bz2",
    {0, 10, 110, 828},  # 命名空间 id，可以在转储文件开头找到
)

for _ in map(partial(page_handler, wtp), wtp.get_all_pages([0])):
    pass
```

基本操作如下：
* 从转储文件中提取模板、模块和其他页面，并将它们保存到 SQLite 文件
* 启发式分析哪些模板需要在解析前预扩展，以便能够理解页面结构（这无法检测到调用输出影响解析结构的 wikitext 的 Lua 代码的模板）。这些初始步骤一起称为"第一阶段"。
* 再次处理页面，为每个页面调用页面处理函数。页面处理函数可以提取、解析并以其他方式处理页面，并且对转储中定义的模板和 Lua 宏拥有完全访问权限。这可以在多个进程中并行调用页面处理函数。页面处理函数调用返回的值返回给调用者。这称为第二阶段。

大部分功能隐藏在 `Wtp` 对象之后。
`WikiNode` 对象用于表示由 `Wtp.parse()` 函数返回的解析树。`NodeKind` 是用于编码 `WikiNode` 类型的枚举类型。

### class Wtp

```python
def __init__(
    self,
    db_path: Optional[Union[str, Path]] = None,
    lang_code="en",
    template_override_funcs: Dict[str, Callable[[Sequence[str]], str]] = {},
    project: str = "wiktionary",
):
```

初始化函数通常可以不带参数调用，但识别以下参数：
* `db_path` 可以是 `None`，在这种情况下将在 `/tmp` 下创建临时数据库文件，或者包含转储文件的页面文本和其他数据的数据库文件路径。
  您可能会设置此参数的原因有两个：
  1) 您在 `/tmp` 上没有足够的空间（英文转储文件需要 3.4G），
  或 2) 用于测试。
  如果您指定路径且存在现有的数据库文件，则使用该文件，这将消除第一阶段所需的时间（这对于测试非常重要，允许相对快速地处理单个页面）。
  在这种情况下，您不应该调用 `Wtp.process()`，而应该使用 `Wtp.reprocess()` 或只是调用 `Wtp.expand()` 或 `Wtp.parse()` 在其他来源上获取的 wikitext（例如，从某些文件）。
  如果文件不存在，您将需要调用 `Wtp.process()` 来解析转储文件，这将在第一阶段初始化数据库文件。如果您想重新创建数据库，应该先删除旧文件。
* `lang_code` - 转储文件的语言代码。
* `template_override_funcs` - 用于覆盖扩展模板文本的 Python 函数。
* `project` - "wiktionary" 或 "wikipedia"。

```python
def read_by_title(
    self, title: str, namespace_id: Optional[int] = None
) -> Optional[str]:
```

从缓存文件读取指定标题的页面内容。通常不需要显式调用此函数，因为 `Wtp.process()` 和 `Wtp.reprocess()` 通常会自动加载页面。此函数不会自动调用 `Wtp.start_page()`。

参数为：
* `title` - 要读取的页面标题
* `namespace_id` - 命名空间 id 编号，如果 `title` 没有命名空间前缀（如 `Template:`），则需要此参数。

这返回页面内容作为字符串，如果页面不存在则返回 `None`。

```python
def parse(
    self,
    text: str,
    pre_expand=False,
    expand_all=False,
    additional_expand=None,
    do_not_pre_expand=None,
    template_fn=None,
    post_template_fn=None,
) -> WikiNode:
```

将 wikitext 解析为解析树（`WikiNode`），可选地扩展 wikitext 中的一些或所有模板和 Lua 宏（使用缓存文件中为模板和宏添加的定义，由 `Wtp.process()` 或 `Wtp.add_page()` 调用添加）。

必须在调用此函数之前调用 `Wtp.start_page()` 函数来设置页面标题（这可能会被模板、Lua 宏和错误消息使用）。`Wtp.process()` 和 `Wtp.reprocess()` 函数将自动调用它。

这接受以下参数：
* `text` (str) - 要解析的 wikitext
* `pre_expand` (boolean) - 如果设置为 `True`，启发式检测到的影响解析的模板（例如，扩展为表开始或结束标签或列表项）将在解析前自动扩展。这些模板使用的任何 Lua 宏也可能被调用。
* `expand_all` - 如果设置为 `True`，在解析前扩展 wikitext 中的所有模板和 Lua 宏。
* `additional_expand` (set 或 `None`) - 如果提供了此参数，它应该是需要在其他选项指定的模板之外扩展的模板名称集合（即，在 `pre_expand` 为 `True` 时启发式检测到的模板之外，或者如果为 false 时仅这些；如果 `expand_all` 设置为 `True`，则此选项无意义）。

这返回解析树。有关用于表示解析树的 `WikiNode` 类的文档，见下文。

```python
def node_to_wikitext(self, node)
```

将解析树的一部分转换回 wikitext。
* `node` (`WikiNode`, str, 这些的列表/元组) - 这是要转换回 wikitext 的解析树部分。我们也允许字符串和列表，因此可以直接使用 `node.children` 作为参数。


```python
def expand(self, text, template_fn=None, post_template_fn=None,
           pre_expand=False, templates_to_expand=None,
           expand_parserfns=True, expand_invoke=True)
```

扩展给定 Wikitext 中选定的模板、解析函数和 Lua 宏。这可以选择性地扩展一些或所有模板。这还可以捕获任何模板的参数和/或扩展，以及用自定义扩展代替默认扩展。

必须在调用此函数之前调用 `Wtp.start_page()` 函数来设置页面标题（这可能会被模板和 Lua 宏使用）。`Wtp.process()` 和 `Wtp.reprocess()` 将自动调用它。页面标题也用于错误消息中。

参数如下：
* `text` (str) - 要扩展的 wikitext
* `template_fn` (function) - 如果设置，这将作为 `template_fn(name, args)` 被调用，其中 `name` (str) 是模板名称，`args` 是包含模板参数的字典。位置参数（和带有数字名称的命名参数）将在字典中拥有整数键，而其他命名参数将以其名称作为键。所有与参数对应的值都是字符串（在它们被扩展之后）。此函数可以返回 `None` 以导致模板以正常方式扩展，或返回将代替模板扩展使用的字符串。这可以返回 `""`（空字符串）以将模板扩展为空。这还可以捕获模板名称及其参数。
* `post_template_fn` (function) - 如果设置，这将作为 `post_template_fn(name, ht, expansion)` 在模板以正常方式扩展后被调用。这可以返回 `None` 以使用默认扩展，或返回字符串以使用该字符串作为扩展。这还可以用于捕获模板、其参数和/或其扩展。
* `pre_expand` (boolean) - 如果设置为 `True`，所有启发式确定需要在解析前扩展的模板将被扩展。
* `templates_to_expand` (`None` 或 set 或 dictionary) - 如果设置，这些模板将在其他指定要扩展的模板之外进行扩展。如果提供字典，其键将作为要扩展的模板名称。如果此值未设置或为 `None`，则所有模板将被扩展。
* `expand_parserfns` (boolean) - 通常， wikitext 解析函数将被扩展。这可以设置为 `False` 以防止解析函数扩展。
* `expand_invoke` (boolean) - 通常，`#invoke` 解析函数（调用 Lua 模块）将与其他解析函数一起扩展。这可以设置为 `False` 以防止 `#invoke` 解析函数的扩展。

```python
def start_page(self, title)
```

此函数应在开始处理新页面或文件之前调用。这将保存页面标题（这经常被模板、解析函数和 Lua 宏访问）。页面标题也用于错误消息中。

`Wtp.process()` 和 `Wtp.reprocess()` 函数将在为每个页面调用页面处理函数之前自动调用此函数。在处理从其他来源获取的 wikitext 时，需要手动调用它。

参数如下：
* `title` (str) - 页面标题。对于正常页面，通常没有前缀。模板通常具有 `Template:` 前缀，Lua 模块具有 `Module:` 前缀，也使用其他前缀（例如，`Thesaurus:`）。这不关心名称的形式，但一些解析函数会。

```python
def start_section(self, title)
```

设置页面当前部分的标题。这由 `Wtp.start_page()` 自动重置为 `None`。部分标题仅用于错误、警告和调试消息中。

参数为：
* `title` (str) - 部分的标题，或 `None` 以清除它。


```python
def start_subsection(self, title)
```

设置页面当前部分的当前子部分的标题。这由 `Wtp.start_page()` 和 `Wtp.start_section()` 自动重置为 `None`。子部分标题仅用于错误、警告和调试消息中。

参数为：
* `title` (str) - 子部分的标题，或 `None` 以清除它。

```python
def add_page(self, title: str, namespace_id: int, body: Optional[str] = None,
             redirect_to: Optional[str] = None, need_pre_expand: bool = False,
             model: str = "wikitext") -> None:
```

此函数用于添加页面、模板和模块进行处理。如果使用了 `Wtp.process()`，通常不需要使用此函数；然而，这可以用于添加模板和页面以进行测试或其他特殊处理需求。

参数为：
* `title` - 要添加的页面标题（正常页面通常在标题中没有前缀，模板以 `Template:` 开头，Lua 模块以 `Module:` 开头）
* `namespace_id` - 命名空间 id
* `body` - 页面、模板或模块的内容
* `redirect_to` - 重定向页面的标题
* `need_pre_expand` - 如果页面是需要解析前扩展的模板，设置为 `True`。
* `model` - 页面的模型值（正常页面和模板通常为 `wikitext`，Lua 模块为 `Scribunto`）

在调用 `Wtp.add_page()` 后，需要调用 `Wtp.analyze_templates()` 函数才能扩展或解析页面（最好只在添加所有页面和模板后调用一次）。

```python
def analyze_templates(self)
```

分析缓存文件中的模板定义，并确定哪些应该在解析前预扩展，因为它们对文档结构有显著影响。例如，Wiktionary 中的一些模板扩展为表开始标签、表结束标签或列表项，如果它们在解析前被扩展，解析结果通常要好得多。实际扩展仅当 `Wtp.expand()` 或 `Wtp.parse()` 的 `pre_expand` 或其他参数告诉它们这样做时才会发生。

分析是启发式的，并不保证找到所有此类模板。特别是，它无法检测到调用输出 wikitext 控制结构的 Lua 模块的模板（例如，Wiktionary 中有几个模板调用输出列表项的 Lua 代码）。此类模板可能需要手动识别并指定为要扩展的附加模板。幸运的是，似乎此类模板相对较少，至少在 Wiktionary 中是这样。

此函数由 `Wtp.process()` 在阶段 1 结束时自动调用。显式调用仅在应用程序使用过 `Wtp.add_page()` 时才需要。

### 错误处理

此模块中的各种函数，包括 `Wtp.parse()` 和 `Wtp.expand()` 可能会生成错误和警告。这些将显示在 `stdout` 上，并收集在 `Wtp.errors`、`Wtp.warnings` 和 `Wtp.debugs` 中。这些字段将包含字典列表，其中每个字典描述一个错误/警告/调试消息。字典可以有以下键（并非所有键都存在）：
* `msg` (str) - 错误消息
* `trace` (str 或 `None`) - 可选的堆栈跟踪，说明错误发生的位置
* `title` (str) - 发生错误的页面标题
* `section` (str 或 `None`) - 发生错误的部分
* `subsection` (str 或 `None`) - 发生错误的子部分
* `path` (str 的元组) - 由标题、模板名称、解析函数名称或 Lua 模块/函数名称组成的路径，提供有关在扩展或解析期间错误发生位置的信息。

包含错误消息的字段将由每次调用 `Wtp.start_page()` 清除（包括 `Wtp.process()` 和 `Wtp.reprocess()` 期间的隐式调用）。因此，`page_handler` 函数通常将这些列表与从页面提取的任何信息一起返回，并且它们可以从这些函数返回的迭代器返回值中收集在一起。`Wtp.to_return()` 函数对此可能有用。

以下函数可用于报告错误。这些也可以在应用程序代码中从 `page_handler` 函数以及 `template_fn` 和 `post_template_fn` 函数中调用，以统一方式报告错误、警告和调试消息。

```python
def error(self, msg, trace=None)
```

报告错误消息。错误将添加到 `Wtp.errors` 列表并打印到 stdout。参数为：
* msg (str) - 错误消息（不需要包含页面标题或部分）
* trace (str 或 `None`) - 可选的堆栈跟踪，提供有关错误发生位置更多信息

```python
def warning(self, msg, trace=None)
```

报告警告消息。警告将添加到 `Wtp.warnings` 列表并打印到 stdout。参数与 `Wtp.error()` 相同。

```python
def debug(self, msg, trace=None)
```

报告调试消息。消息将添加到 `Wtp.debugs` 列表并打印到 stdout。参数与 `Wtp.error()` 相同。

```python
def to_return(self)
```

生成包含来自 `Wtp` 的错误、警告和调试消息的字典。这通常应在 `page_handler` 函数末尾调用，并将值与从该页面提取的任何数据一起返回。错误列表由 `Wtp.start_page()` 重置（包括 `Wtp.process()` 和 `Wtp.reprocess()` 期间的隐式调用），因此应该为每个页面保存（例如，通过此调用）。（鉴于页面处理的并行性，它们不能简单地积累在子进程中。）

返回的字典包含以下键：
* `errors` - 描述任何错误消息的字典列表
* `warnings` - 描述任何警告消息的字典列表
* `debugs` - 描述任何调试消息的字典列表。

### class WikiNode

`WikiNode` 类表示解析树节点，由 `Wtp.parse()` 返回。此对象可以打印或转换为字符串，并将显示适合调试目的的人类可读格式（至少对于小解析树）。

`WikiNode` 对象具有以下字段：
* `kind` (NodeKind，见下文) - 节点的类型。这决定了如何解释其他字段。
* `children` (list) - 节点的内容。这通常用于节点具有任意大小内容的情况，例如子部分、列表项/子列表、其他 HTML 标签等。
* `args` (list 或 str，取决于 `kind`) - 节点的直接参数。这用于模板、模板参数、解析函数参数和链接参数，在这种情况下这是一个列表。对于某些节点类型（例如，列表、列表项和 HTML 标签），这直接是一个字符串。
* `attrs` - 包含 HTML 属性或定义列表定义（在 `def` 键下）的字典。

### class NodeKind(enum.Enum)

`NodeKind` 类型是用于解析树（`WikiNode`）节点类型的枚举值。当前使用以下值（通常这些需要由 `NodeKind.` 前缀，例如，`NodeKind.LEVEL2`）：
* `ROOT` - 解析树的根节点。
* `LEVEL2` - 2 级标题 (==)。`args` 字段包含标题，`children` 字段包含此部分内的任何内容。
* `LEVEL3` - 3 级标题 (===)
* `LEVEL4` - 4 级标题 (====)
* `LEVEL5` - 5 级标题 (=====)
* `LEVEL6` - 6 级标题 (======)
* `ITALIC` - 斜体，内容在 `children` 中
* `BOLD` - 粗体，内容在 `children` 中
* `HLINE` - 水平线（没有参数或子节点）
* `LIST` - 表示列表。每个列表和子列表将以这种类型的节点开始。`args` 将包含用于打开列表的前缀（例如，`"##"` - 注意这直接作为字符串存储在 `args` 中）。列表项将存储在 `children` 中。
* `LIST_ITEM` - `LIST` 节点的子节点中的列表项。`args` 是用于打开列表项的前缀（与 `LIST` 节点相同）。列表项的内容（包括任何可能的子列表）在 `children` 中。如果列表是定义列表（即，前缀以 `";"` 结尾），则 `children` 包含要定义的项标签，`definition` 包含定义。
* `PREFORMATTED` - 解释标记的预格式文本。内容在 `children` 中。这用于 wikitext 中以空格开头的行。
* `PRE` - 不解释标记的预格式文本。内容在 `children` 中。这在 wikitext 中由 `&lt;pre&gt;...&lt;/pre&gt;` 指示。
* `LINK` - 内部 wikimedia 链接（`[[...]]` 在 wikitext 中）。链接参数在 `args` 中。此标签也用于媒体包含。带有尾随单词的链接在链接后立即结束，尾随部分在 `children` 中。
* `TEMPLATE` - 模板调用（跨引用）。模板名称在第一个参数中，模板参数在随后的 `args` 参数中。不使用 `children` 字段。在 wikitext 中，模板标记为 `{{name|arg1|arg2|...}}`。
* `TEMPLATE_ARG` - 模板参数。参数名称在 `args` 的第一个项目后跟随任何后续参数（通常最多两个项目，但我也见过有多个参数的参数 - 可能在那些模板定义中有错误）。不使用 `children` 字段。在 wikitext 中，模板参数标记为 `{{{name|defval}}}`。
* `PARSER_FN` - 解析函数调用。这也用于内置变量，如 `{{PAGENAME}}`。解析函数名称在 `args` 的第一个元素中，解析函数参数在随后的元素中。
* `URL` - 外部 URL。第一个参数是 URL。第二个可选参数（在 `args` 中）是显示文本。不使用 `children` 字段。
* `TABLE` - 表格。内容在 `children` 中。在 wikitext 中，表格编码为 `{| ... |}`。
* `TABLE_CAPTION` - 表格标题。这只能出现在 `TABLE` 下。内容在 `children` 中。`attrs` 字段包含给定表格的任何 HTML 属性的字典。
* `TABLE_ROW` - 表格行。这只能出现在 `TABLE` 下。内容在 `children` 中（正常情况的内容将是 `TABLE_CELL` 或 `TABLE_HEADER_CELL` 节点）。`attrs` 字段包含给定表格行的任何 HTML 属性的字典。
* `TABLE_HEADER_CELL` - 表格表头单元格。这只能出现在 `TABLE_ROW` 下。内容在 children 中。`attrs` 字段包含给定表格行的任何 HTML 属性的字典。
* `TABLE_CELL` - 表格单元格。这只能出现在 `TABLE_ROW` 下。内容在 `children` 中。`attrs` 字段包含给定表格行的任何 HTML 属性的字典。
* `MAGIC_WORD` - MediaWiki 魔法字。魔法字直接分配给 `args` 作为字符串（即在列表中）。不使用 `children`。魔法字的示例是 `__NOTOC__`。
* `HTML` - HTML 标签（或匹配的 HTML 标签对）。`args` 是 HTML 标签的名称（直接在列表中且总是没有斜杠）。`attrs` 设置为包含标签的任何 HTML 属性的字典。HTML 标签的内容在 `children` 中。

## 预期性能

这通常可以每处理器核心每秒处理几个 Wiktionary 页面，包括扩展所有模板、Lua 宏、解析整个页面和分析解析。在多线程机器上，这通常可以每秒处理几十到几百个页面，具体取决于速度和大小的核心数。

大部分处理工作量用于扩展 Lua 宏。您可以选择不扩展 Lua 宏，但它们在 Wiktionary 中被广泛使用并且对重要信息有用。扩展模板和 Lua 宏允许更稳健和完整的数据提取，但并不便宜。

## 贡献和错误报告

请在 github 上创建问题来报告错误或进行贡献！
