# 函数级与句级开发指南

本指南面向维护解析规则、表结构和句级输出的开发者。修改前先运行相关测试；完整测试入口是 `python -m unittest discover -s tests -v`。

## 函数级开发指南

### 入口、配置与数据源

| 位置 | 核心函数/类 | 职责与修改注意事项 |
| --- | --- | --- |
| `wikipedia_parser.py` | `build_parser`、`_run`、`main` | CLI、统一配置、writer 创建和 WTP DB 准备。新增 CLI 参数时同时传递到 `_run` 所用组件。 |
| `pipeline/pipeline_config.py` | `load_pipeline_config` | 读取 `sqlserver`、`source`、`wtp`；路径相对配置文件解析。 |
| `pipeline/pipeline_config.py` | `output_table_names_for_dump` | 从 dump 文件名提取八位日期，统一生成 `_YYYYMMDD` 表后缀。改命名规则必须同步 checkpoint、compare 工具与测试。 |
| `pipeline/xml_source.py` | `XmlBatchSource`、`build_latest_record` | 流式读取 `.xml.bz2`，每页仅保留最新 revision。不要把整个 dump 收集到列表。 |
| `pipeline/xml_source.py` | `SQLServerWriter` | 原始记录批量写入；与 checkpoint 的 raw 确认协作。 |

### 解析与组件

| 位置 | 核心函数/类 | 职责与修改注意事项 |
| --- | --- | --- |
| `pipeline/engine.py` | `extract_and_templatize` | 用 `mwparserfromhell` 提取组件，普通 wikilink 保持在 `text_process`，其余组件替换为内部占位符。新增组件类型时更新 `COMPONENT_TYPES`、提取分支、对应测试和预期表。 |
| `pipeline/engine.py` | `_component_id` | 生成 `<type>-<page_id>-<seq>`。同一页面同类型序号必须稳定且不可重复。 |
| `pipeline/engine.py` | `remove_format_blocks` | 移除特定格式块，避免已提取的占位符残留在最终文本中。 |
| `pipeline/engine.py` | `should_skip_wtp_for_toc` | 判断参考文献等节是否跳过 WTP/句级展开。标题比较为大小写无关，并接受 `Title:Child`。 |
| `pipeline/engine.py` | `build_bundle`、`process_batch` | worker 的主要边界：输入原始行，输出可 pickle 的 bundle 和错误统计。异常应转换为 `parse_error`，不要让单篇文章杀死整个 batch。 |

### WTP、writer 与编排

| 位置 | 核心函数/类 | 职责与修改注意事项 |
| --- | --- | --- |
| `pipeline/wtp_integration.py` | `initialize_wtp_worker` | 每个 worker 初始化独立 WTP 实例；不要在父进程创建后传入子进程。 |
| `pipeline/wtp_integration.py` | `analyze_wikitext`、`expand_to_text` | 执行 WTP 分析、展开和可见文本转换；错误作为返回值而非终止管线。 |
| `pipeline/engine.py` | `ProcessedTextWriter` | `revision_id` MERGE upsert。增加 processed 字段时更新 `_COLS`、建表 SQL、MERGE SQL 与输入 bundle。 |
| `pipeline/engine.py` | `ComponentWriter` | 统一维护全部组件表，按页面 delete-then-insert；新增类型无需新 writer，但需确保 `COMPONENT_TYPES` 完整。 |
| `pipeline/engine.py` | `SectionsWriter`、`ParagraphsWriter`、`WtpIntermediateWriter`、`SentencesWriter` | 维护层次表，按 revision delete-then-insert。新唯一约束名必须基于实际表名，防止版本表冲突。 |
| `pipeline/engine.py` | `FanoutWriter`、`_run_core` | writer 协议和并发核心。新 sink 应实现 `add_rows`、`flush`、`close` 后传给 `FanoutWriter`。 |
| `pipeline/checkpoint.py` | `SQLServerCheckpointStore`、`CheckpointCoordinator` | 只有 raw 与 processed 都提交后才记录终态；修改提交顺序时不得破坏这个屏障。 |

## 句级开发指南

句级逻辑位于 `pipeline/sentence_extractor.py`。输入是 `Paragraph` 列表，输出是 `Sentence` 列表；每个句子保存 `section_no`、`paragraph_no`、`sentence_no`、`toc`、`raw_text` 和 `text`。

### 处理顺序

1. `extract_sentences` 先移除已经被提取的 `ref` 占位符。
2. 若前一段以 `:` 或 `：` 结尾且下一段只有列表项，会先将两段合并。
3. `_prepare_lists_for_segmentation` 将连续列表折叠成结构化文本，用不透明 token 保护内部标点，防止 `sentencex` 把列表拆碎。
4. 常规段落调用 `sentencex.segment(lang, text)`；异常或空结果时，整段退化为一句。
5. `_restore_list_tokens` 恢复列表内容；普通 wikilink 由 `_process_wikilinks` 转为 `[[显示文本↓目标]]`。无显示文本的 `[[Target]]` 变为 `[[Target↓Target]]`。
6. `skip_tocs` 命中的参考文献等节不分句、不改写 wikilink，整段保留为一句。

`sentence_no` 在同一 section 内连续编号，不会因 paragraph 边界重置。任何新规则都必须保持该编号语义。

### 修改规则时的建议

- 修改 wikilink 格式：重点覆盖无 `|`、一个 `|`、多个 `|`、空目标和显示文本的情况。
- 修改列表逻辑：覆盖嵌套 `*`/`#`/`;`/`:`、列表内句末标点、冒号引导语与列表跨段合并。
- 修改跳过节：测试大小写、`References:Books` 这种子节和跳过后后续段落的行为。
- 修改 sentencex 调用：保留异常 fallback，否则单个语言规则异常会导致整页失败。

相关测试集中在 `tests/test_sentence_wikilinks.py`、`tests/test_list_pipeline.py` 和 `tests/test_skip_wtp_sections.py`。先运行对应文件，再运行完整测试集。
