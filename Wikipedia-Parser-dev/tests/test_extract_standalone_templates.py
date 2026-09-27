"""测试提取仅由一个 wikitext 模板组成的段落。"""

from __future__ import annotations

import ast
import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "extract_standalone_templates.py"


def _function(name: str):
    tree = ast.parse(SCRIPT_PATH.read_text(encoding="utf-8"))
    return next(
        (
            child
            for child in tree.body
            if isinstance(child, ast.FunctionDef)
            and child.name == name
        ),
        None,
    )



def _load_function(name: str):
    node = _function(name)
    assert node is not None
    namespace: dict[str, object] = {}
    exec(
        compile(ast.Module(body=[node], type_ignores=[]), str(SCRIPT_PATH), "exec"),
        namespace,
    )
    function = namespace[name]
    assert callable(function)
    return function


def _load_script_module():
    module_name = "extract_standalone_templates_test_module"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class StandaloneTemplateTest(unittest.TestCase):
    def test_returns_name_and_text_for_one_complete_template_paragraph(self) -> None:
        node = _function("standalone_template")
        self.assertIsNotNone(node)
        standalone_template = _load_function("standalone_template")

        self.assertEqual(
            (
                "see also",
                "{{see also|List of cities in the People's Republic of China}}",
            ),
            standalone_template(
                "  {{see also|List of cities in the People's Republic of China}}  \n"
            ),
        )
        self.assertEqual(
            ("outer", "{{outer|value={{inner|x}}}}"),
            standalone_template("{{outer|value={{inner|x}}}}"),
        )

    def test_rejects_non_template_or_mixed_paragraphs(self) -> None:
        node = _function("standalone_template")
        self.assertIsNotNone(node)
        standalone_template = _load_function("standalone_template")

        for value in (
            None,
            "",
            "Article {{citation needed}} text",
            "{{foo}}\n\nArticle text",
            "{{foo}} {{bar}}",
        ):
            with self.subTest(value=value):
                self.assertIsNone(standalone_template(value))


class ProgressLineTest(unittest.TestCase):
    def test_shows_completed_source_rows_and_percentage(self) -> None:
        node = _function("progress_line")
        self.assertIsNotNone(node)
        progress_line = _load_function("progress_line")

        self.assertEqual(
            "[###############---------------]  50.0% 500/1000 rows, 12 templates",
            progress_line(500, 1000, 12),
        )


class WriteTemplatesTest(unittest.TestCase):
    def test_uses_wtp_location_as_template_identity(self) -> None:
        class Cursor:
            def __init__(self) -> None:
                self.parameters: list[tuple[object, ...]] = []
                self.statement = ""

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, statement, *parameters):
                self.statement = statement
                self.parameters.append(parameters)
                return self

            def fetchone(self):
                return ("INSERT",)

        class Connection:
            def __init__(self) -> None:
                self.cursor_instance = Cursor()

            def cursor(self):
                return self.cursor_instance

            def commit(self) -> None:
                pass

        connection = Connection()
        script = _load_script_module()

        self.assertEqual(
            1,
            script.write_templates(
                connection,
                {(7, 2, 3): ("see also", "{{see also|Example}}")},
            ),
        )
        self.assertEqual(
            (7, 2, 3, "see also", "{{see also|Example}}"),
            connection.cursor_instance.parameters[0],
        )
        self.assertNotIn("<> source.[template_text]", connection.cursor_instance.statement)

    def test_ignores_unchanged_template_without_merge_output(self) -> None:
        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, *_args):
                return self

            def fetchone(self):
                return None

        class Connection:
            def __init__(self) -> None:
                self.cursor_instance = Cursor()
                self.commits = 0

            def cursor(self):
                return self.cursor_instance

            def commit(self) -> None:
                self.commits += 1

        connection = Connection()
        script = _load_script_module()

        self.assertEqual(
            0,
            script.write_templates(
                connection,
                {(7, 2, 3): ("see also", "{{see also}}")},
            ),
        )
        self.assertEqual(1, connection.commits)


class EnsureTargetTableTest(unittest.TestCase):
    def test_rebuilds_legacy_hash_table_with_location_primary_key(self) -> None:
        class Cursor:
            def __init__(self) -> None:
                self.statement = ""

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, statement):
                self.statement = statement

        class Connection:
            def __init__(self) -> None:
                self.cursor_instance = Cursor()

            def cursor(self):
                return self.cursor_instance

            def commit(self) -> None:
                pass

        connection = Connection()
        script = _load_script_module()
        script.ensure_target_table(connection)

        statement = connection.cursor_instance.statement
        self.assertIn("DROP TABLE [dbo].[template]", statement)
        self.assertIn("t.name <> N'nvarchar'", statement)
        self.assertIn(
            "PRIMARY KEY ([page_id], [section_no], [paragraph_no])", statement
        )
        self.assertNotIn("[template_hash]", statement)


if __name__ == "__main__":
    unittest.main()
