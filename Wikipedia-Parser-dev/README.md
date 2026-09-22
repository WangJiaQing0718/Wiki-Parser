# Wikipedia 解析器

单次读取 Wikipedia XML 转储，**同时**将原始数据存入数据库，并为每个 ns=0 的文章运行 **章节 -> 每章节组件 -> 段落 -> WTP 中间 -> 句子** 的转换链，生成：一个 `wiki_processed` 行（将每个提取的组件原位替换为其组件 ID 的维基文本），各类组件表（包含提取的信息框/表格/维基链接/外部链接/文件/图片/参考文献），以及明细表 `wiki_sections` / `wiki_paragraph` / `wiki_wtp_intermediate` / `wiki_sentence`。维基列表保留在段落流中，等待句子阶段的列表处理。

```
                          ┌──▶ latest      (原始副本，固定模式)
simplewiki-*.xml.bz2 ──单次通过──┤
                          │        ┌──▶ processed   (text_process: 保留标记，组件 -> ID)
                          └──解析─┤
                                   ├──▶ wiki_component_{信息框，表格，维基链接，外部链接，文件，图片，参考文献}
                                    └──▶ 章节 / 段落 / wtp_中间 / 句子
```

转储只读取一次，也只解压一次；原始写入和解析写入并行运行。

## 安装

```bash
# 安装本地打补丁的 ProjectB；不要从 PyPI 替换它。
python3 -m pip install --no-build-isolation -e ../wikitextprocessor
python3 -m pip install -r requirements.txt
```

## 数据库配置

```bash
cp config.example.json config.json     # 填写 SQL Server / WTP / 转储设置
```

`config.json` 包含三个部分：`source.dump_file`、`sqlserver` 和 `wtp`。
`sqlserver` 包含 `host`、`port`（默认 1433）、`user`、`password`、`database`，
`schema`（默认 `dbo`），以及可选的 `table`（原始表名，默认
`latest`）。`wtp.db_path` 选择版本化的 WTP SQLite 数据库。

## 使用

```bash
python3 wikipedia_parser.py \
  --config config.json \
  --workers 8
```

处理 SQL Server 中已存在的行（无需导入/重新编译转储）：

```bash
python3 wikipedia_parser.py --source-table latest \
  --config config.json --max-pages 1000
```

当未提供位置参数转储文件时，使用配置的转储。位置参数转储文件会覆盖 `source.dump_file`；`--source-table` 使用 SQL 输入。WTP 数据库以只读方式打开，并禁用维基数据 HTTP 回退。每个进程拥有独立的 WTP/Lua 上下文。

运行时，四个速度栏同步刷新 -- `Read`（读取页面）/ `Parse`（解析）/ `Write`（处理后的表）/ `Raw`（原始表）：

```
Read : 12000 [00:14, 820 页/秒]
Parse: 11900 [00:14, 815 行/秒，失败=0]
Write: 11800 [00:14, 810 行/秒]
Raw  : 12000 [00:14, 820 行/秒]
```

先尝试处理 100 个页面：

```bash
python wikipedia_parser.py --config config.json --workers 16 --max-pages 10000
```

参数：

| 参数 | 默认值 | 描述 |
| --- | --- | --- |
| `dump_file` | 配置值 | 可选转储路径覆盖（`*.xml.bz2` 或 `*.xml`） |
| `--config` | 必需 | 包含 `source`、`sqlserver` 和 `wtp` 的统一 JSON |
| `--source-table` | 关闭 | 现有的 SQL Server 原始源，如 `latest` |
| `--processed-table` | `processed` | 处理后的表（每篇文章一行；`text_process`） |
| `--table-prefix` | `component` | 组件表前缀；每种类型写入到 `<prefix>_<type>` |
| `--sections-table` | `sections` | 章节表（每章节一行；按 `revision_id` 每刷新一次删除再插入） |
| `--paragraphs-table` | `paragraph` | 段落表（每段落一行；按 `revision_id` 每刷新一次删除再插入） |
| `--wtp-intermediate-table` | `wtp_intermediate` | WTP 输入和每段落的展开维基文本；按 `revision_id` 每刷新一次删除再插入 |
| `--sentences-table` | `sentence` | 句子表（每句子一行；按 `revision_id` 每刷新一次删除再插入） |
| `--workers` | CPU 核心数 | 解析进程数 |
| `--chunk-size` | 500 | 每批页面数 |
| `--write-batch` | 500 | 两个表的批量 upsert 大小 |
| `--max-pages` | 全部 | 要处理的最大页面数 |
| `--namespace` | 0 | 仅处理这些命名空间（可重复）；默认是 0（文章） |
| `--all-namespaces` | 关闭 | 处理所有命名空间（覆盖 `--namespace`） |
| `--task-timeout` | 300 | 每批解析超时（秒），防止进程挂起；0 禁用 |
| `--db-timeout` | 300 | DB 查询超时（秒）；0 禁用 |
| `--db-login-timeout` | 60 | DB 连接/登录超时（秒） |

原始表名取自配置的 `table`（默认 `latest`）。

### XML 输出表名

对于 XML 导入，运行中写入的每个数据库表都使用转储名称中包含的八位日期进行版本管理。XML 文件名必须包含 `-<YYYYMMDD>-`；例如，`simplewiki-20260801-pages-articles.xml.bz2` 会写入：

```
wiki_latest_[20260801]
wiki_processed_[20260801]
wiki_component_infobox_[20260801]
wiki_sections_[20260801]
wiki_parse_checkpoint_[20260801]
```

相同的后缀应用于每个组件和层级表。来自配置和表相关 CLI 选项的值在添加后缀之前保持基础名称。`--source-table` 是只读输入，不派生后缀；其输出表选项按指定精确使用。

## 段落 WTP 扩展

对于每个章节，MWP 提取现有的组件行，并用现有的 `♣  ♣  ♣  <组件 ID>♣  ♣  ♣` 占位符替换已识别的组件。不应用任何组件名称的允许/拒绝列表。结果段落由 MWP 进行结构分析，由 WTP 展开（模板、解析函数、`#invoke`、Lua/Scribunto），并转换为可见文本。`paragraph` 除了其现有的修订/页面/章节/段落键之外，还存储 `page_title`、`raw_wikitext`、最终 `text` 和 `parse_error`。`wtp_intermediate` 使用相同的段落自然键存储 MWP 替换的 `wtp_input` 和 WTP 的 `expanded_wikitext`（在可见文本转换之前），每段落的 WTP/Lua 错误都记录在这两个表中，而不停止页面的其余部分或批次。

## 处理后的表 + 组件表

对于每个 ns=0 的文章，`engine.extract_and_templatize()` 生成：

- 一个 **`processed`** 行，其 `text_process` 是原始维基文本，**每个提取的组件原位替换为 `♣  ♣  ♣  <组件 ID>♣  ♣  ♣`**（club 分隔符标记占位符；其他标记保留）。**维基链接是例外**：它们被提取到它们的表中，但在 `text_process` 中 **以原始 `[[...]]` 形式保留原位**（不替换）；
- **七个组件表**中的行，每个包含一个组件的提取内容，通过相同的 ID 键索引。

**提取是按优先级严格包含的**。块（信息框/表格）包含其中的所有内容；内联组件（维基链接、外部链接、`file`（用于 `[[File:...]]`）、`image`（用于 `[[Image:...]]`）、`ref`（用于 `<ref>...</ref>`））仅在顶层提取。因此，信息框/表格内的 `[[链接]]`、`[[File:...]]` 或 `<ref>` 作为原始维基文本保留在该组件内（不为其创建单独的行）；`<ref>` 或 `[[File:...]]` 同样包含其嵌套的内容。列表块不是组件，也受到嵌套组件提取的保护，因此它们的原始层次结构到达句子阶段的列表处理器。

`text_process` 因此是将块/外部链接原位替换为 `♣  ♣  ♣  id♣  ♣  ♣`
占位符（保留原始行结构 -- 占位符周围不添加额外空行）的文章，维基链接和列表保留原始形式。列表因此到达 WTP，然后到达句子提取器的列表保护/渲染阶段：

```
text_process:  "'''粗体''' [[柏林]] 说 {{引用}}，见 ♣  ♣  ♣  external_links-12-0002♣  ♣  ♣"
component_wikilinks:  wikilinks-12-0003 -> "柏林"   (已提取，但 [[柏林]] 保留在文本中)
component_external_links: external_links-12-0002 -> "https://..."  (由其 club 分隔占位符替换)
```

**`processed`**（`--processed-table`，每篇文章一行，按 `revision_id` MERGE upsert）：
`revision_id`（主键）、`page_id`、`page_title`、`namespace`、`text_process`、`parse_error`。

**组件表** `<prefix>_<type>`（默认前缀 `component`），所有形状相同：

```sql
CREATE TABLE component_infobox (
    [id]           BIGINT IDENTITY(1,1) PRIMARY KEY,
    [page_id]      BIGINT NOT NULL,          -- 所属页面
    [infobox_id]   NVARCHAR(128) NOT NULL,   -- infobox-<page_id>-<seq>，如 infobox-12-0001
    [infobox_text] NVARCHAR(MAX) NULL,       -- 提取内容
    CONSTRAINT [UQ_component_infobox_id] UNIQUE ([infobox_id])
);
CREATE INDEX [IX_component_infobox_page] ON component_infobox ([page_id]);
```

| 表 | `<type>_text` 内容 | 每篇文章行数 |
| --- | --- | --- |
| `component_infobox` | 每个 `{{Infobox ...}}` 的原始维基文本 | 每个信息框一行 |
| `component_table` | 每个表格 (`{| ... |}` / `<table>`) 的原始维基文本 | 每个表格一行 |
| `component_wikilinks` | 内部维基链接目标标题（仅顶层） | 每个散文 `[[...]]` 一行 |
| `component_external_links` | 外部链接 URL（仅顶层） | 每个散文外部链接一行 |
| `component_file` | 原始 `[[File:...]]` 维基文本 | 每个顶层文件链接一行 |
| `component_image` | 原始 `[[Image:...]]` 维基文本 | 每个顶层图片链接一行 |
| `component_ref` | 原始 `<ref>...</ref>` 维基文本 | 每个顶层参考文献一行 |

`<type>_id` 是 `<type>-<page_id>-<seq>`，其中 `<seq>` 是 **页面上的 1-based 出现顺序**（0001 = 该页上的第一个信息框等），因此 `text_process` 中的 ID 与组件行匹配，且源页面可恢复（`WHERE page_id = 12`）。

> **所有写入路径都是幂等的（可安全重复运行）。** 原始表和处理后的表是按 `revision_id` 的 `MERGE`
> upsert；三个层级表和七个组件表是 **每刷新一次删除再插入**（一个事务：删除以下键的所有行再重新插入 -- 对于层级表按 `revision_id`，对于组件表按 `page_id`），因此
> 重新运行相同的转储会原位替换行而无重复，且来自更改解析链的行会自动清理。数据库中也强制执行唯一性：
> 原始/处理后表上的 `revision_id` 主键，层级表上的 `UNIQUE(revision_id, ...)`，以及每个
> 组件表上的 `<type>_id` 唯一约束加 `page_id` 索引（因此刷新 DELETE 可以索引查找，重复 ID 会报错）。
> 非维基文本页面（Lua/JSON/CSS/JS）保持原样，无组件。
> 解析失败会计数（`parse_failed=N`）并记录在 `processed.parse_error` 中。

## 层级表（章节 / 段落 / 句子）

`engine.build_bundle()` 运行与 API-Parser 提取阶段相同的转换链（`lib/` 提取器模块的独立副本，保持功能对齐）：

1. **章节**（`section_extractor.extract_sections`）：按 `== Title ==` 分割（恰好两个等号；
   `===` 及更深的不分割）。第一个 `==` 之前的内容是 `section_no=1, toc="Summary"`；
   没有标题的文章是整个 Summary 章节。
2. **每章节**：组件提取 + `remove_format_blocks()`（移除 `<templatestyles>` /
   `<div>` 格式块）。一旦到达 `toc` 在跳过集（`references` /
   `other websites` / `related pages` / `more reading` / `category` / `external links` /
   `footnotes` / `notes`）中的章节，其段落及之后的所有段落都只保留段落：
   不进行 MWP 分析、WTP 展开或句子分割。文章的 `text_process` 是每章节处理后的文本以
   空行连接；每个来自 `== Title ==` 标题的章节都以前缀添加该原始标题行，因此章节边界
   在 `text_process` 中保持可见（Summary 章节没有标题）。
3. **段落**（`paragraph_extractor.extract_paragraphs`）：每个章节内按空行分割；
   子章节标题（`===` 及更深）从文本中移除并更新冒号连接的
   `toc` 路径（如 `Overview:History`）；`paragraph_no` 每章节从 1 重新开始。
4. **句子**（`sentence_extractor.extract_sentences`）：`sentencex` 句子分割；先移除 club 分隔的参考文献
   占位符；列表块在分割前折叠为保护结构，因此内部标点不会分割它们；维基链接渲染为
   `[[target↓target]]`（`[[A|B]]` → `[[B↓A]]`）；`sentence_no` 是 **章节内的** 1-based 顺序。

表结构（所有表按 `revision_id` 键索引；**每刷新一次删除再插入**：修订的所有行在一个事务中删除并重新插入）：

| 表 | 每行 | 自然键 | 显著字段 |
| --- | --- | --- | --- |
| `sections` | 章节 | `(revision_id, section_no)` | `page_title`、`toc`、`raw_text`、`text` |
| `paragraph` | 段落 | `(revision_id, section_no, paragraph_no)` | `page_title`、`toc`、`raw_wikitext`、`text`、`parse_error` |
| `wtp_intermediate` | 段落 WTP 边界 | `(revision_id, section_no, paragraph_no)` | `page_title`、`toc`、`wtp_input`、`expanded_wikitext`、`parse_error` |
| `sentence` | 句子 | `(revision_id, section_no, paragraph_no, sentence_no)` | `page_title`、`toc`、`raw_text`、`text` |

每个层级表都有 `id` BIGINT IDENTITY 主键；不保留时间戳列。

```sql
-- 页面的章节 -> 段落 -> 句子树
SELECT s.toc, p.paragraph_no, p.text AS paragraph, t.sentence_no, t.text AS sentence
FROM sections AS s
JOIN paragraph AS p
  ON p.revision_id = s.revision_id AND p.section_no = s.section_no
JOIN sentence AS t
  ON t.revision_id = p.revision_id AND t.section_no = p.section_no
  AND t.paragraph_no = p.paragraph_no
WHERE s.page_id = @page_id
ORDER BY s.section_no, p.paragraph_no, t.sentence_no;
```

示例查询：

```sql
-- 重建文章骨架及其信息框
SELECT p.text_process, i.infobox_id, i.infobox_text
FROM processed AS p
LEFT JOIN component_infobox AS i ON i.page_id = p.page_id
WHERE p.page_title = N'Foo';

-- 语料库中最常被链接的页面
SELECT wikilinks_text AS target, COUNT(*) AS refs
FROM component_wikilinks
GROUP BY wikilinks_text
ORDER BY refs DESC;
```

## 代码结构

```
wikipedia_parser.py        入口点：CLI + 编排接线
pipeline/
├── xml_source.py          XML 侧：流式批源 + 原始表写入器（固定模式）
├── section_extractor.py   章节分割按 == Title ==（API-Parser 的 lib/ 独立副本）
├── paragraph_extractor.py 空行段落分割 + 子章节 toc 路径
├── sentence_extractor.py  sentencex 句子分割 + 维基链接/列表处理
└── engine.py              处理引擎：build_bundle 链 + 写入器
                           （ProcessedTextWriter + ComponentWriter + Sections/Paragraphs/SentencesWriter）
```

- `engine.py` 的核心，`_run_core(batches, processed_writer, raw_writer=...)`，与"批源"解耦：
  本项目使用 XML 流驱动它并附加原始写入器。
- `engine.build_bundle()` 运行章节 -> 每章节组件 -> 段落 ->
  句子链；`engine.FanoutWriter` 将每个文章包扇入到 `ProcessedTextWriter`（处理后的
  表）、`ComponentWriter`（七个组件表）和 `SectionsWriter` / `ParagraphsWriter` /
  `SentencesWriter`（三个层级表）。
- 原始表模式固定，对应转储字段，由 `xml_source.SQLServerWriter` 写入。
