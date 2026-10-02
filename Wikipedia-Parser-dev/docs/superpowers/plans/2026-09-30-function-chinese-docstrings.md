# 全函数中文 Docstring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 `Wikipedia-Parser-dev` 的所有 Python 函数和方法提供准确、清楚的中文 docstring，且不改变执行逻辑。

**Architecture:** 以 Python docstring 作为唯一的函数级说明格式。按生产模块、命令与基准脚本、测试模块三个独立批次修改；每批后通过 AST 检查、编译和对应测试确认行为未改变。

**Tech Stack:** Python 3.10+、标准库 `ast`、`unittest`、`compileall`。

**Spec:** `docs/superpowers/specs/2026-09-30-function-chinese-docstrings-design.md`

## Global Constraints

- 覆盖全部函数、方法和嵌套辅助函数。
- 每个函数体的第一条语句使用中文 docstring。
- 保留清楚的现有中文说明；将英文或不清楚的说明改写为中文。
- 测试函数以“验证：”开头。
- 不修改函数签名、控制流、SQL、配置、测试断言或运行行为。

## Review Focus

- AST 遍历应覆盖嵌套函数和类方法，不能只检查模块级函数。
- `__init__`、`close`、`flush` 等短方法也必须有说明。
- docstring 插入后，装饰器、缩进、`from __future__` 导入和三引号内容必须保持语法正确。
- 测试说明必须描述断言行为，不能只重复测试函数名。
- 所有 `unittest` 测试必须仍可通过。

---

### Task 1: 生产模块函数说明

**Files:**
- Modify: `wikipedia_parser.py`
- Modify: `pipeline/checkpoint.py`
- Modify: `pipeline/engine.py`
- Modify: `pipeline/paragraph_extractor.py`
- Modify: `pipeline/pipeline_config.py`
- Modify: `pipeline/run_logging.py`
- Modify: `pipeline/section_extractor.py`
- Modify: `pipeline/sentence_extractor.py`
- Modify: `pipeline/wtp_database.py`
- Modify: `pipeline/wtp_integration.py`
- Modify: `pipeline/xml_source.py`

**Interfaces:**
- Consumes: 现有函数签名和调用关系。
- Produces: 每个生产函数、方法和嵌套函数的中文 docstring；不新增运行时接口。

- [ ] 为每个函数体的第一条语句补充或改写中文 docstring。
- [ ] 使用 AST 扫描生产模块，确认每个 `FunctionDef` 与 `AsyncFunctionDef` 均有 docstring。
- [ ] 运行 `python -m compileall -q wikipedia_parser.py pipeline`，确认退出码为 0。
- [ ] 运行 `python -m unittest discover -s tests -p 'test_*.py' -v`，确认生产代码相关测试仍通过。

### Task 2: 命令与基准脚本函数说明

**Files:**
- Modify: `bench/read_parse.py`
- Modify: `bench/write_latency.py`

**Interfaces:**
- Consumes: 现有命令行参数与基准 Writer 协议。
- Produces: 每个基准函数、模拟 Writer 方法和入口函数的中文 docstring。

- [ ] 为每个函数和方法补充中文 docstring，说明其基准或模拟职责。
- [ ] 使用 AST 扫描 `bench/`，确认没有缺失 docstring 的函数。
- [ ] 运行 `python -m compileall -q bench`，确认退出码为 0。

### Task 3: 测试函数说明

**Files:**
- Modify: `tests/test_checkpoint.py`
- Modify: `tests/test_component_placeholders.py`
- Modify: `tests/test_list_pipeline.py`
- Modify: `tests/test_mwp_analysis.py`
- Modify: `tests/test_run_logging.py`
- Modify: `tests/test_sentence_wikilinks.py`
- Modify: `tests/test_skip_wtp_sections.py`
- Modify: `tests/test_table_identifier_quoting.py`
- Modify: `tests/test_unified_config.py`
- Modify: `tests/test_wtp_database.py`

**Interfaces:**
- Consumes: 现有 `unittest` 测试名称、断言与模拟对象。
- Produces: 每个测试、测试辅助函数和模拟对象方法的中文 docstring；测试逻辑不变。

- [ ] 为每个测试函数添加以“验证：”开头的中文 docstring；为辅助函数和模拟方法说明职责。
- [ ] 使用 AST 扫描 `tests/`，确认所有函数和方法都有 docstring。
- [ ] 运行 `python -m unittest discover -s tests -v`，确认全部测试通过。

### Task 4: 全范围验收

**Files:**
- Verify: `wikipedia_parser.py`
- Verify: `pipeline/**/*.py`
- Verify: `bench/**/*.py`
- Verify: `tests/**/*.py`

**Interfaces:**
- Consumes: Tasks 1–3 的 docstring 修改。
- Produces: 覆盖范围与运行行为均已验证的最终状态。

- [ ] 运行 AST 检查，递归扫描上述所有 Python 文件；缺失 docstring 的函数数量必须为 0。
- [ ] 运行 `python -m compileall -q .`，确认退出码为 0。
- [ ] 运行 `python -m unittest discover -s tests -v`，确认全部测试通过。
- [ ] 运行 `git diff --check`，确认没有空白符错误。
