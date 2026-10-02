"""回归测试覆盖版本化表名上的 SQL Server 元数据查找。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PIPELINE_DIR = Path(__file__).resolve().parents[1] / "pipeline"
sys.path.insert(0, str(PIPELINE_DIR))

from engine import (  # noqa: E402
    ParagraphsWriter,
    SectionsWriter,
    SentencesWriter,
    WtpIntermediateWriter,
)
from checkpoint import SQLServerCheckpointStore  # noqa: E402


class VersionedTableMetadataTest(unittest.TestCase):
    def test_hierarchical_unique_constraints_include_the_target_table_name(self) -> None:
        """验证：testhierarchicaluniqueconstraintsincludethe目标数据表name的预期行为。"""
        writers = (
            (SectionsWriter, "wiki_sections_20260801", "rev_secno"),
            (ParagraphsWriter, "wiki_paragraph_20260801", "rev_secno_pno"),
            (WtpIntermediateWriter, "wiki_wtp_intermediate_20260801", "rev_secno_pno"),
            (SentencesWriter, "wiki_sentence_20260801", "rev_secno_pno_sno"),
        )

        for writer_type, table, key_suffix in writers:
            with self.subTest(writer=writer_type.__name__):
                connection = _RecordingConnection()
                with patch("engine.connect", return_value=connection):
                    writer_type(_CONFIG, "dbo", table)

                self.assertIn(
                    f"CONSTRAINT [UQ_{table}_{key_suffix}]",
                    connection.statements[0],
                )

    def test_hierarchical_writers_quote_versioned_names_in_metadata_lookups(self) -> None:
        """验证：testhierarchicalwriters转义带版本namesinmetadatalookups的预期行为。"""
        writers = (
            (SectionsWriter, "wiki_sections_[20260801]"),
            (WtpIntermediateWriter, "wiki_wtp_intermediate_[20260801]"),
            (SentencesWriter, "wiki_sentence_[20260801]"),
        )

        for writer_type, table in writers:
            with self.subTest(writer=writer_type.__name__):
                connection = _RecordingConnection()
                with patch("engine.connect", return_value=connection):
                    writer_type(_CONFIG, "dbo", table)

                expected_object_name = "[dbo].[" + table.replace("]", "]]" ) + "]"
                self.assertIn(
                    f"COL_LENGTH(N'{expected_object_name}', N'page_title')",
                    connection.statements[0],
                )

    def test_checkpoint_uses_a_quoted_versioned_name_for_existence_checks(self) -> None:
        """验证：test检查点usesaquoted带版本nameforexistencechecks的预期行为。"""
        connection = _RecordingConnection()
        table = "wiki_parse_checkpoint_[20260801]"

        with patch("checkpoint.pymssql.connect", return_value=connection):
            SQLServerCheckpointStore(_CONFIG, "dbo", table, "source-id")

        expected_object_name = "[dbo].[" + table.replace("]", "]]" ) + "]"
        self.assertIn(
            f"OBJECT_ID(N'{expected_object_name}', N'U')",
            connection.statements[0],
        )


_CONFIG = {
    "host": "db.example.test",
    "user": "user",
    "password": "password",
    "database": "wiki",
}


class _RecordingConnection:
    def __init__(self) -> None:
        """初始化对象所需的状态和资源。"""
        self.statements: list[str] = []

    def cursor(self) -> "_RecordingCursor":
        """执行cursor的处理逻辑。"""
        return _RecordingCursor(self.statements)

    def commit(self) -> None:
        """执行commit的处理逻辑。"""
        pass


class _RecordingCursor:
    def __init__(self, statements: list[str]) -> None:
        """初始化对象所需的状态和资源。"""
        self.statements = statements

    def __enter__(self) -> "_RecordingCursor":
        """执行enter的处理逻辑。"""
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        """执行exit的处理逻辑。"""
        pass

    def execute(self, statement: str) -> None:
        """执行execute的处理逻辑。"""
        self.statements.append(statement)


if __name__ == "__main__":
    unittest.main()
