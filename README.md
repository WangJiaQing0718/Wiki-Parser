# Wiki Parser

## 0、项目结构

根目录下有三个项目目录，职责互不重叠：

```text
Wiki-Parser-main/
├── WikiData/                 # XML文件与 WTP 数据库
├── Wikipedia-Parser-dev/     # 解析器流水线（本项目的主要代码）
└── wikitextprocessor/        # 第三方 WTP 解析库（本地可编辑副本）
```

| 目录 | 作用 |
| --- | --- |
| `WikiData/` | 存放语料与数据资产：Wikipedia 导出的 `*.xml.bz2` 源数据，以及由它们生成、供 WTP 展开模板和 Lua 用的同名 `*-wtp-full.db`（SQLite）。另含一个独立建库脚本 `build_wtp_database.py`，不包含解析器业务逻辑。 |
| `Wikipedia-Parser-dev/` | 核心流水线：主命令入口 `wikipedia_parser.py`，`pipeline/` 下的配置、WTP 库准备、XML 流式读取、并发调度与各层抽取器，以及 `tests/`、`bench/`、`docs/`。解析结果由这里写入 SQL Server。 |
| `wikitextprocessor/` | 第三方包 wikitextprocessor（简称 WTP）的本地副本，真正的模板与 Lua 展开引擎：`src/wikitextprocessor/` 提供 `Wtp` 类、wikitext 解析器、解析器函数和 Lua 执行环境。|

`Wikipedia-Parser-dev` 读取 `WikiData` 中的数据并调用 `wikitextprocessor` 完成展开；

## 1、WTP 数据库构建

`WikiData/*.xml.bz2` 是 Wikipedia 导出的源数据；同名的 `*-wtp-full.db`
是供 `wikitextprocessor`（WTP）展开模板和 Lua 模块使用的 SQLite 缓存。
它不是解析结果写入的 SQL Server 数据库。每份新的 XML 压缩包都应使用
同一份源文件单独生成对应的 WTP 数据库。

### 前置条件
- 已将新的 `*-pages-articles.xml.bz2` 放入 `WikiData/`。
- 为生成的 SQLite 数据库预留充足磁盘空间。现有约 354 MB 的压缩包生成的 WTP 数据库约为 1.47 GB。

### 使用指南

1. 手动执行得到 `*‑wtp‑full.db` 文件。【Code: `WikiData/build_wtp_database.py`】
  ```sh
  cd WikiData
  python build_wtp_database.py simplewiki-20260901-pages-articles.xml.bz2
  ```

2. 执行解析命令时自动从 config.json 中的构建 `*‑wtp‑full.db` 文件。【Code: `Wikipedia-Parser-dev/pipeline/wtp_database.py` 】

### 说明：
  - 默认只导入命名空间 {0,10,828}（文章、文件、模板），可通过修改 `Wikipedia-Parser-dev/pipeline/wtp_database.py` 中的 `WTP_NAMESPACE_IDS` 参数自定义。
  - lang_code / project 由 config.json 中的 wtp 字段决定；在 CLI 入口中固定为 "en" / "wikipedia"。
  - 若目标 *‑wtp‑full.db 已存在，ensure_wtp_database 会直接返回该路径，不会重建，防止误删已有语料。
  - 在完整流水线中，wikipedia_parser.py 只在启动时调用一次 ensure_wtp_database；随后所有 worker 进程使用该 SQLite 数据库，支持并发解析而不会互相覆盖。