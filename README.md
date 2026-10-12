# Wiki Parser

## 0、项目结构

仓库主要由数据、解析流水线和 WTP 解析库组成。解析流水线读取 `WikiData/` 中的 Wikipedia 转储，调用 `wikitextprocessor/` 展开模板和 Lua 模块，并将解析结果写入 SQL Server。

```text
Wiki-Parser/
├── WikiData/                         # Wikipedia XML 转储与 WTP SQLite 数据库
│   └── build_wtp_database.py         # 从 XML 转储构建或复用 WTP SQLite 数据库
├── Wikipedia-Parser-dev/             # 解析器入口、流水线、基准脚本和运行日志
│   ├── pipeline/                     # 配置、读取、调度和内容抽取模块
│   ├── bench/                        # 性能基准脚本
│   ├── logs/                         # 运行日志
│   └── wikipedia_parser.py           # 命令行入口
├── wikitextprocessor/                # WTP 第三方解析库的本地源码副本
│   └── src/wikitextprocessor/        # WTP 解析器、模板和 Lua 执行实现
├── compare.py                        # Streamlit 数据对照工具
└── requirements.txt                  # Python 依赖
```

`WikiData/` 保存源转储和 WTP 使用的 SQLite 数据库；`Wikipedia-Parser-dev/` 负责读取、解析和写入；`wikitextprocessor/` 提供模板与 Lua 展开能力。`compare.py` 用于对照查看解析数据，不属于解析流水线本身。


## 1、安装依赖

需要 Python 3.10 或更高版本。

在仓库根目录（包含 `requirements.txt` 与 `wikitextprocessor/` 的 `Wiki-Parser/` 目录）执行：

```powershell
python -m pip install -r requirements.txt
```

`requirements.txt` 以 `-e ./wikitextprocessor` 安装本地 WTP 包；若环境无法下载构建依赖，改用 `python -m pip install --no-build-isolation -r requirements.txt`。

## 2、WTP 数据库构建

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
