# 全函数中文 Docstring 规范

## 目标

为 `Wikipedia-Parser-dev` 下全部 Python 函数和方法补齐清楚、简洁的中文 docstring，帮助学习和阅读；不改变任何运行逻辑、接口或测试断言。

## 覆盖范围

- 生产代码：命令入口与 `pipeline/` 模块。
- 测试代码：`tests/` 中的测试方法、测试辅助函数与模拟对象方法。
- 基准脚本：`bench/` 中的函数和方法。
- 包括私有函数、类方法和嵌套函数。

## 写法

- 每个函数或方法的函数体第一条语句为中文 docstring。
- 生产函数说明主要职责、关键输入或输出，以及必要的副作用。
- 测试函数以“验证：”开头，说明被验证的行为。
- 私有辅助函数说明其在当前模块中的用途。
- 已有清楚的中文 docstring 保留；英文或不清楚的说明改写为中文。
- 不为显而易见的参数逐项重复类型标注，不添加与实现重复的行内注释。

## 验证

- 使用 AST 检查每个函数或方法均有 docstring。
- 运行 `python -m compileall -q .`。
- 在 `Wikipedia-Parser-dev` 下运行 `python -m unittest discover -s tests -v`。
