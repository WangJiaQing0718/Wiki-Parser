"""回归测试覆盖 Compare 的版本化 SQL 表标识符。"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


COMPARE_PATH = Path(__file__).resolve().parents[2] / "compare.py"


def _compare_nodes() -> list[ast.stmt]:
    return ast.parse(COMPARE_PATH.read_text(encoding="utf-8")).body


class CompareTableNamesTest(unittest.TestCase):
    def test_compare_uses_the_current_versioned_tables(self) -> None:
        values = {
            node.targets[0].id: ast.literal_eval(node.value)
            for node in _compare_nodes()
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id.endswith("_TABLE")
        }

        suffix = "_[20260801]"
        self.assertEqual("wiki_paragraph" + suffix, values["PARAGRAPH_TABLE"])
        self.assertEqual("wiki_sentence" + suffix, values["SENTENCE_TABLE"])
        self.assertEqual("wiki_latest" + suffix, values["LATEST_TABLE"])
        self.assertEqual("wiki_wtp_intermediate" + suffix, values["INTERMEDIATE_TABLE"])

    def test_identifier_quoting_escapes_the_version_bracket(self) -> None:
        quote_node = next(
            (
                node
                for node in _compare_nodes()
                if isinstance(node, ast.FunctionDef) and node.name == "quote_identifier"
            ),
            None,
        )
        self.assertIsNotNone(quote_node)
        assert quote_node is not None
        namespace: dict[str, object] = {}
        exec(compile(ast.Module(body=[quote_node], type_ignores=[]), str(COMPARE_PATH), "exec"), namespace)

        quote_identifier = namespace["quote_identifier"]
        assert callable(quote_identifier)
        self.assertEqual(
            "[wiki_paragraph_[20260801]]]",
            quote_identifier("wiki_paragraph_[20260801]"),
        )


if __name__ == "__main__":
    unittest.main()
