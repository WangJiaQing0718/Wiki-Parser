# Wiki Parser

## 1、WTP 数据库构建

`WikiData/*.xml.bz2` 是 Wikipedia 导出的源数据；同名的 `*-wtp-full.db`
是供 `wikitextprocessor`（WTP）展开模板和 Lua 模块使用的 SQLite 缓存。
它不是解析结果写入的 SQL Server 数据库。每份新的 XML 压缩包都应使用
同一份源文件单独生成对应的 WTP 数据库。

### 前置条件
- 已将新的 `*-pages-articles.xml.bz2` 放入 `WikiData/`。
- 已安装 Python 3.10 或更高版本。
- 为生成的 SQLite 数据库预留充足磁盘空间。现有约 354 MB 的压缩包生成的
  WTP 数据库约为 1.47 GB。

### 使用指南

1. 手动执行得到 `*‑wtp‑full.db` 文件。【Code: `WikiData/build_wtp_database.py`】
  ```sh
  cd WikiData
  python build_wtp_database.py simplewiki-20260901-pages-articles.xml.bz2
  ```

2. 执行解析命令时自动从 config.json 中的构建 `*‑wtp‑full.db` 文件。【Code: `Wikipedia-Parser-dev/pipeline/wtp_database.py` 】：
包含 wtp_database_path_for_dump（表名推导）、build_wtp_database（原子化导入、临时文件保护）以及 ensure_wtp_database（复用已存在或自动建库）。

### 使用边界：
  - dump_file 必须是 *.xml.bz2，且位于 WikiData/ 路径下。
  - 默认只导入命名空间 {0,10,828}（文章、文件、模板），可通过修改 `Wikipedia-Parser-dev/pipeline/wtp_database.py` 中的 `WTP_NAMESPACE_IDS` 参数自定义。
  - lang_code / project 由 config.json 中的 wtp 字段决定；在 CLI 入口中固定为 "en" / "wikipedia"。
  - 若目标 *‑wtp‑full.db 已存在，ensure_wtp_database 会直接返回该路径，不会重建，防止误删已有语料。
  - 在完整流水线中，wikipedia_parser.py 只在启动时调用一次 ensure_wtp_database；随后所有 worker 进程只读使用该 SQLite 数据库，支持并发解析而不会互相覆盖。
  - 生产环境建议先在小范围转储上跑一遍 build_wtp_database 验证路径与命名空间无误，再对全量 dump 执行 python -m wikipedia_parser ... --config config.json。




### 在 WikiData 中生成 WTP 数据库

在项目根目录执行：先进入 `WikiData/`，然后传入 XML 文件名（`*.xml.bz2`）。

```powershell
cd .\WikiData
python .\build_wtp_database.py simplewiki-20260901-pages-articles.xml.bz2
```

脚本会复用同名的 `*-wtp-full.db`；如果它不存在，则自动从 XML 转储构建，
不会覆盖已有的数据库。