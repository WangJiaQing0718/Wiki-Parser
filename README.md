# Wiki Parser

## 从 XML 压缩包生成 WTP 数据库

`WikiData/*.xml.bz2` 是 Wikipedia 导出的源数据；同名的 `*-wtp-full.db`
是供 `wikitextprocessor`（WTP）展开模板和 Lua 模块使用的 SQLite 缓存。
它不是解析结果写入的 SQL Server 数据库。每份新的 XML 压缩包都应使用
同一份源文件单独生成对应的 WTP 数据库。

### 前置条件

- 已将新的 `*-pages-articles.xml.bz2` 放入 `WikiData/`。
- 已安装 Python 3.10 或更高版本。
- 为生成的 SQLite 数据库预留充足磁盘空间。现有约 354 MB 的压缩包生成的
  WTP 数据库约为 1.47 GB。

### 生成步骤

在 PowerShell 中进入 `Wikipedia-Parser-dev/`，安装项目使用的本地
`wikitextprocessor`：

```powershell
cd .\Wikipedia-Parser-dev
python -m pip install --no-build-isolation -e ..\wikitextprocessor
```

将下面命令中的 `新的文件` 替换为实际文件名（不要包含扩展名），然后执行：

```powershell
python -c "from pathlib import Path; from wikitextprocessor import Wtp; from wikitextprocessor.dumpparser import process_dump; dump=Path(r'..\WikiData\新的文件.xml.bz2').resolve(); db=Path(r'..\WikiData\新的文件-wtp-full.db').resolve(); assert dump.is_file(), dump; assert not db.exists(), f'目标数据库已存在：{db}'; wtp=Wtp(db_path=db, lang_code='en', project='wikipedia'); process_dump(wtp, str(dump), {0, 10, 828}); wtp.close()"
```

命令会读取 XML 压缩包，并将文章（命名空间 `0`）、模板（`10`）和 Lua
模块（`828`）写入新的 `.db`。模板和 Lua 模块是正确展开文章内容所必需的。

> 该命令不会覆盖已有的 `.db`；若目标文件已存在，会停止并提示路径。

### 更新解析器配置

生成完成后，编辑 `Wikipedia-Parser-dev/config.json`，将源文件和 WTP
数据库路径改为新文件。例如：

```json
{
  "source": {
    "dump_file": "../WikiData/新的文件.xml.bz2"
  },
  "wtp": {
    "db_path": "../WikiData/新的文件-wtp-full.db",
    "version": "新的文件对应版本",
    "lang_code": "en",
    "project": "wikipedia",
    "read_only": true,
    "wikidata_offline": true
  }
}
```

保留 `config.json` 中原有的 `sqlserver` 配置；只更新上例中的 `source` 和
`wtp` 两个部分即可。`WikiData/` 下的 XML 和 `.db` 是本地大文件，不应提交到
Git 仓库。
