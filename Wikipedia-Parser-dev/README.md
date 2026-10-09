# Wikipedia Parser

## 1. 项目树

```text
Wikipedia-Parser-dev/
├── wikipedia_parser.py          # 主命令入口
├── config.example.json          # 统一配置模板
├── pipeline/
│   ├── pipeline_config.py       # 读取配置、生成版本化表名
│   ├── wtp_database.py          # 为 XML 准备同版本 WTP SQLite DB
│   ├── xml_source.py            # 流式 XML 读取和原始表写入
│   ├── engine.py                # 解析、并发调度和所有派生 writer
│   ├── section_extractor.py     # wikitext → section
│   ├── paragraph_extractor.py   # section → paragraph
│   ├── component_extractor.py   # 提取 8 类组件并改写为占位符（wikilink 除外）；列表保护
│   ├── sentence_extractor.py    # paragraph → sentence
│   ├── wtp_integration.py       # WTP/Lua 的 worker 级封装
│   ├── checkpoint.py            # resume 状态持久化
│   └── run_logging.py           # 本次运行的失败日志文件
├── bench/                       # 性能基准脚本，不参与正常导入
└── logs/                        # 运行期生成；每次运行的失败日志
```

将 Wikipedia XML dump 单遍读取、解压和解析，并把原始版本记录与结构化解析结果并行写入 SQL Server。输入文件名中的八位日期会成为输出表名后缀，例如 `simplewiki-20260901-pages-articles.xml.bz2` 默认写入 `wiki_latest_20260901`、`wiki_processed_20260901` 和 `wiki_component_*_20260901`。

```text
① db 文件检测（建库阶段）────┐
                           ▼
simplewiki-*.xml.bz2 ──single pass──▶ ② XML 文件读取 ──▶ 读取循环内过滤/截断后分流
                                                              ├──▶ wiki_latest_YYYYMMDD
                                                              └──▶ ③ 进程池 ──▶ 逐页解析
                                                                     │
                                                                     └──▶ ④ 分节 ──▶ ⑤ 分段 ──▶ ⑥ 组件提取
                                                                                                   │
                                                                     ┌─────────────────────────────┘
                                                                     ▼
                                                               ⑦ 模板展开 ──▶ ⑧ 分句
```

各步的编号说明如下（同一阶段内用 `（阶段号-序号）` 编号，最后一列为实现函数）。表中 ① 是流水线启动前的建库阶段，②③ 由主进程的读取/汇总/写入线程驱动，④–⑧ 在 worker 进程内逐页执行。

### ① DB 文件检测（建库阶段，先于流水线）

| 情形 | 行为 | 实现 |
| --- | --- | --- |
| XML 文件同名 `*-wtp-full.db` 命中 | 直接复用（不重建、不覆盖） | `wtp_database_path_for_dump` / `ensure_wtp_database` |
| XML 文件同名 `*-wtp-full.db` 未命中 | 从该 XML 建库后复用 | `build_wtp_database` |

### ④ 分节

| 编号 | 功能 | 实现 |
| --- | --- | --- |
| （4-1） | **标题切分**：只认行首恰好两个等号，`=== 子标题 ===` 及更深层级不在此处切分，留给 ⑤ | `_SECTION_HEADER_RE` |
| （4-2） | **Summary 兜底**：首个 `==` 之前的内容作为 `section_no=1`、`toc="Summary"`；全文没有 `==` 时整篇即一个 Summary | `extract_sections` |
| （4-3） | **标题行留存**：标题行不进 `Section.text`，单独存进 `Section.heading`，供 ⑦ 侧拼 `text_process` 时补回，保证占位符文本仍能定位节边界 | `Section.heading` |

### ⑤ 分段

| 编号 | 功能 | 实现 |
| --- | --- | --- |
| （5-1） | **空行切段**：以一个及以上空行（容忍空格/Tab、CRLF）为界；单个换行保留在段内，避免拆散列表项和折行文本 | `_PARAGRAPH_SEPARATOR_RE` |
| （5-2） | **子标题路径**：`===` 及更深的子标题从正文中移除，改用以冒号连接的 `toc`（如 `References:Books`）。该路径是后面 WTP 跳过的判定依据 | `_split_block_by_subsections` |
| （5-3） | **原文关联**：组件占位符保留其源文本映射；段落在组件提取后生成，`paragraph.raw_wikitext` 通过映射恢复对应原文，避免按段落序号错配 | `build_bundle` |

### ⑥ 组件提取

| 编号 | 功能 | 实现 |
| --- | --- | --- |
| （6-0） | **过滤注释内容**：先用 `mwparserfromhell` 解析 wikitext 并移除注释节点，注释中的模板、链接等内容不参与组件提取，也不会进入正文占位符结果 | `extract_and_templatize` |
| （6-1） | **成对模板区块**：识别 `*start/*end`、`*top/*bottom`、`*begin` 这类同族成对模板，跨节点扫描至配对结束，整块（连同内部内容）收为 `independent_template`；只登记最外层，嵌套块不重复收录 | `_outermost_template_block_ranges` |
| （6-2） | **独立模板行判定**：物理行上除该模板只剩空白，或整个段落只由模板节点组成时收为 `independent_template`；按行与按段落两个判定互补，缺一会漏掉"整段仅模板、但模板自身含换行"的情形 | `_template_only_line_node_indexes` / `_template_only_paragraph_node_indexes` |
| （6-3） | **指定类型提取**：`{{Infobox...}}` → `infobox`；`<table>` 标签节点 → `table`；其余模板节点保留原文，继续参与 ⑦ 展开 | `extract_and_templatize` 阶段一 |
| （6-4） | **行内组件提取**：按顶层节点分派——`<ref>` → `ref`、`[[File:...]]` → `file`、`[[Image:...]]` → `image`、`[url 显示文本]` → `external_links`；普通 `[[...]]` 只登记进 `wikilinks`，**正文保留原始 `[[...]]`**，留到 ⑧ 再格式化 | `extract_and_templatize` 阶段二 |
| （6-5） | **列表保护**：连续以 `*` `#` `;` `:` 开头的行先整体换成不透明 token，避免列表内容被误判成组件（token 与正文冲突时自动加前缀重试），本阶段结束原样还原 | `_protect_list_blocks` |

与 ⑥ 有关的两个输出细节：

- 组件以 `(组件 ID, 组件文本)` 登记，正文位置改写为占位符 `♣  ♣  ♣  <type>-<page_id>-<4 位序号>♣  ♣  ♣`。
- **各类型的组件文本并不一致**：`infobox` / `table` / `independent_template` / `file` / `image` / `ref` 是整段原始 wikitext，`external_links` 只有 URL（显示文本丢弃），`wikilinks` 只有链接目标。序号按类型各自从 1 递增，故同页可同时存在 `ref-123-0001` 与 `file-123-0001`。

### ⑦ 模板展开（逐段执行）

| 编号 | 功能 | 实现 |
| --- | --- | --- |
| （7-1） | **页面上下文**：每页只调用一次 `Wtp.start_page(page_title)`，为页面级解析器函数建立状态 | `begin_page` |
| （7-2） | **MWP 校验**：用 mwparserfromhell 试解析该段，只取"能否解析"这一个信号，**不涉及 WTP**；失败写入该段 `parse_error`，但不会阻塞后续流程 | `analyze_wikitext` |
| （7-3） | **模板/Lua 展开**：按 `expand_timeout` 秒展开；超时或抛异常时该段降级——直接把展开前的输入当作结果，并把异常信息拼进 `parse_error` | `expand_to_text` → `Wtp.expand` |
| （7-4） | **可见文本转换**：去 HTML 注释、`<ref>`、`[[File:/Image:]]`、`Category:`、行首 `=` 号、粗斜体与魔术字；再用 HTML 解析器丢掉 `<style>/<script>/error` 等块并在块级标签处断行，最后压缩空白。普通 `[[...]]` 在此**故意保留** | `expanded_wikitext_to_text` / `_VisibleTextParser` |

### ⑧ 分句

| 编号 | 功能 | 实现 |
| --- | --- | --- |
| （8-1） | **移除 ref 占位符**：先删掉 `ref-*` 占位符，避免引用标记混进句子或干扰分句 | `_remove_ref_placeholders` |
| （8-2） | **合并冒号引导语**：上一段以 `:` / `：` 结尾、且本段只由列表行构成时，两段并为一段再分句，使"引导语 + 列表"留在同一句 | `_is_list_only_text` |
| （8-3） | **列表保护**：连续列表行折叠成层级文本并换成 `WIKILISTBLOCKTOKEN…END.`；前一行以冒号结尾时 token 直接续在该行后，子项用 `: (…)`、同级用 `;`/`,` 渲染，末尾补句号，保证整块列表不被拆分 | `_prepare_lists_for_segmentation` / `_render_list_block` |
| （8-4） | **sentencex 分句**：调用 `sentencex.segment(lang, text)`（`lang` 默认 `"en"`）；`sentence_no` 在 section 内递增并记录 `paragraph_no`；抛异常或没产出句子时整段兜底成一句 | `extract_sentences` |
| （8-5） | **还原与格式化**：token 换回列表正文；普通链接格式化为 `[[目标↓目标]]`（`[[目标\|显示]]` → `[[显示↓目标]]`，含多个竖线时保持原样）。`raw_text` 保留还原后、格式化前的文本 | `_restore_list_tokens` / `_process_wikilinks` |

### 跨 ⑦⑧ 的段落级跳过

段落的 `toc` 首个冒号前若命中 `references` / `external links` / `other websites` / `related pages` / `more reading` / `category` / `footnotes` / `notes`，则**该段及其后所有段落**都不做 ⑦ 和 ⑧：不写 `wiki_wtp_intermediate`，不产生 `wiki_sentence`，`parse_error` 为空；这些段落在 `wiki_paragraph.text` 中保留组件占位符形式。跳过由 `engine.build_bundle` 的 `wtp_skipped` 单向闩锁控制（`should_skip_wtp_for_toc` 只比较首个冒号前的根标题）。`sentence_extractor.extract_sentences` 的 `skip_tocs` 参数是预留接口，生产调用未传入。

## 使用

在仓库根目录（`Wiki-Parser-main/`，即包含 `requirements.txt` 与 `wikitextprocessor/` 的目录）执行：

```powershell
python -m pip install -r requirements.txt
```

`requirements.txt` 以 `-e ./wikitextprocessor` 安装本地 WTP 包；若环境无法下载构建依赖，改用 `python -m pip install --no-build-isolation -r requirements.txt`。

## 配置文件

复制示例配置并填写 SQL Server 连接信息：

```powershell
cd .\Wikipedia-Parser-dev
Copy-Item .\config.example.json .\config.json
```

当前代码使用统一配置文件 `config.json`，只读取 `sqlserver`、`source`、`wtp` 三个节点。`config.example.json` 的完整参数说明如下：

```json
{
  "sqlserver": {
    "host": "10.0.0.10",        // SQL Server IP
    "port": 1433,               // 端口
    "user": "sa",               // 登录用户名
    "password": "PASSWORD",     // 登录密码
    "database": "wiki",         // 目标数据库名
    "schema": "dbo"             // 表所在 schema，缺省 dbo
  },
  "source": {
    "dump_file": "../WikiData/simplewiki-20260901-pages-articles.xml.bz2"
                                // XML 转储路径，必填；相对路径以 config.json 所在目录为基准
                                // 文件名里的 20260901 决定所有输出表的日期后缀
  },
  "wtp": {
    "lang_code": "en",          // 转储语言；使用 WTP 时用到
    "project": "wikipedia",     // 站点类型；使用 WTP 时用到
    "expand_timeout": 60        // 单段落模板/Lua 展开超时秒数，超时该段降级
  }
}
```

## 启动命令

使用配置中的 dump：
```powershell
python .\wikipedia_parser.py --config .\config.json
```
若没有单独执行 `WikiData\build_wtp_database.py` 代码，上述命令会自动创建 .db 文件，实测 simplewiki ——> SQLite.db 约 120s， enwiki ——> SQLite.db 约 6.5h。

运行时会显示四个独立进度条：

- `Read`：已通过命名空间过滤、送入流水线的记录数（`--max-pages` 按物理 `<page>` 计数，故实际扫描的页数可能更多）。
- `Parse`：已由进程池完成解析的记录数。
- `Write`：已写入 processed 及其派生结果的记录数。
- `Raw`：已写入原始表的记录数（与 `Read` 是同一批记录，无额外的命名空间放宽）。

simplewiki 完整处理约 60 分钟。

## 命令行参数说明

全部选项都定义在 `wikipedia_parser.py` 的 `build_parser()` 中，`python .\wikipedia_parser.py -h` 可看到同样内容。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `dump_file` | 取 `source.dump_file` | 可选位置参数，覆盖配置中的转储路径。相对路径以当前工作目录为基准（配置里的 `dump_file` 则以 `config.json` 所在目录为基准）。 |
| `--config PATH` | 必填 | 统一 JSON 配置文件；文件不存在时直接报错退出。 |
| `--processed-table NAME` | `wiki_processed` | processed 表基础名。 |
| `--table-prefix NAME` | `wiki_component` | 组件表基础名前缀。 |
| `--sections-table NAME` | `wiki_sections` | section 表基础名。 |
| `--paragraphs-table NAME` | `wiki_paragraph` | paragraph 表基础名。 |
| `--wtp-intermediate-table NAME` | `wiki_wtp_intermediate` | WTP 中间表基础名。 |
| `--sentences-table NAME` | `wiki_sentence` | sentence 表基础名。 |
| `--workers N` | CPU 核心数 | 解析进程数，必须 ≥ 1。 |
| `--chunk-size N` | `500` | XML 读取批大小（每批页数），必须 ≥ 1。 |
| `--write-batch N` | `500` | 各 writer 的批量 upsert 大小，必须 ≥ 1。 |
| `--max-pages N` | 不限 | 最大扫描页数（按物理 `<page>` 元素计数，早于命名空间过滤），必须 ≥ 1。它同时截断原始表与解析链。 |
| `--namespace ID` | `0` | 只处理这些命名空间，可重复指定。过滤发生在读取循环内、分流之前，故原始表与 processed/组件/层次表写入的是同一批页面。 |
| `--all-namespaces` | 关 | 覆盖 `--namespace`，不做命名空间过滤；此时原始表也随之收全部命名空间。 |
| `--task-timeout SEC` | `300` | 单批解析等待上限；`0` 禁用。 |
| `--db-timeout SEC` | `300` | 数据库查询超时；`0` 禁用。 |
| `--db-login-timeout SEC` | `60` | 数据库连接/登录超时，必须 ≥ 1。 |
| `--resume` / `--no-resume` | `--resume` | 是否用 checkpoint 跳过已终态修订版。 |
| `--retry-errors` | 关 | 配合 `--resume`，重新处理 `completed_with_errors` 修订版。 |
| `--checkpoint-table NAME` | `wiki_parse_checkpoint` | checkpoint 表基础名。 |

表名规则：上面所有表名参数都会先补 `wiki_` 前缀（已以 `wiki_` 开头则不加），再追加转储文件名中的 `_YYYYMMDD` 日期后缀。例如 `--processed-table foo` 实际写入的是 `wiki_foo_20260901`，`--table-prefix comp` 得到的是 `wiki_comp_infobox_20260901` 等 8 张组件表。原始表固定使用 `wiki_latest` 基名，并追加日期后缀（例如 `wiki_latest_20260901`），不从配置文件读取表名。

转储文件名必须包含独立的八位日期（形如 `simplewiki-20260901-pages-articles.xml.bz2`），否则启动即报错退出。

另外 `main()` 在解析参数之前就会创建本次运行的日志文件 `logs/parser-<时间>-<pid>.log`。

## 已处理表与组件表

默认只处理 ns=0 文章。命名空间过滤与 `--max-pages` 截断都发生在 `xml_source.XmlBatchSource` 的读取循环内、数据分流之前，因此 `wiki_latest_YYYYMMDD` 与 processed/组件/层次表写入的是**同一批**页面：默认仅 ns=0，且同样受 `--max-pages` 限制——原始表**不是**全命名空间的完整副本，只有使用 `--all-namespaces` 且不设 `--max-pages` 时才是全量。每篇文章在 `wiki_processed_YYYYMMDD` 中有一行，包含 `revision_id`、页面信息、`text_process` 和 `parse_error`。`text_process` 会以组件 ID 占位符替换已提取组件；普通 wikilink 则保留原始 `[[...]]` 形式。

当前实现有 8 类组件表：`infobox`、`table`、`independent_template`、`wikilinks`、`external_links`、`file`、`image`、`ref`。每张表的列固定为自增主键 `id`、`page_id`、`<type>_id`（UNIQUE）和 `<type>_text`；组件 ID 格式为 `<type>-<page_id>-<4 位零填充序号>`（序号从 1 开始，如 `ref-123-0001`）。`file` 与 `image` 分表，列表不产生独立组件表。

processed 表按 `revision_id` 使用 `MERGE` upsert。组件表和层次表采用“按本批页面/修订版删除后再插入”的方式，使内容变化后不会遗留陈旧行；它们不是 append-only 表。

解析异常仍会保留 processed 行，`parse_error` 非空。检查失败记录示例：

```sql
SELECT revision_id, page_id, page_title, parse_error
FROM dbo.wiki_processed_20260901
WHERE parse_error IS NOT NULL
ORDER BY revision_id;
```

## Code Structure

```text
wikipedia_parser.py             CLI、配置加载、writer 编排与运行日志
pipeline/pipeline_config.py     统一配置解析与版本化表名
pipeline/wtp_database.py        为 XML 准备/复用同版本 WTP SQLite DB
pipeline/xml_source.py          XML 流式批源与原始表 writer
pipeline/engine.py              解析编排（build_bundle / process_batch）、数据库 writers 与 _run_core
pipeline/section_extractor.py   wikitext → section
pipeline/paragraph_extractor.py section → paragraph
pipeline/component_extractor.py 组件提取、列表保护与占位符改写
pipeline/sentence_extractor.py  paragraph → sentence
pipeline/wtp_integration.py     进程内 WTP 初始化及文本展开
pipeline/checkpoint.py          SQL Server checkpoint 持久化
pipeline/run_logging.py         本次运行的失败日志文件
bench/                          性能基准脚本（见 bench/README.md），不参与正常导入
development-guide.md            函数级、句级开发指南
```

`engine._run_core` 只依赖批源 iterable 和 writer 的 `add_rows` / `close` 协议（启用 checkpoint 时还会调用 `flush`），因此 XML 来源和输出端可以分别替换。`FanoutWriter` 把同一批解析 bundle 分发到 processed、组件和层次 writer。

