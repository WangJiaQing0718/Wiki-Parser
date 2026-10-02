"""WTP 数据库自动准备行为测试。"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch


PIPELINE_DIR = Path(__file__).resolve().parents[1] / "pipeline"
WTP_SOURCE_DIR = Path(__file__).resolve().parents[2] / "wikitextprocessor" / "src"
sys.path.insert(0, str(PIPELINE_DIR))
sys.path.insert(0, str(WTP_SOURCE_DIR))

from wtp_database import (  # noqa: E402
    build_wtp_database,
    ensure_wtp_database,
    wtp_database_path_for_dump,
)


WIKIDATA_SCRIPT = (
    Path(__file__).resolve().parents[2] / "WikiData" / "build_wtp_database.py"
)


def _load_wikidata_script() -> object:
    """执行加载wikidatascript的处理逻辑。"""
    spec = importlib.util.spec_from_file_location("build_wtp_database", WIKIDATA_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WtpDatabaseTest(unittest.TestCase):
    def test_wikidata_script_resolves_the_given_filename(self) -> None:
        """验证：testwikidatascriptresolvesthegivenfilename的预期行为。"""
        self.assertTrue(WIKIDATA_SCRIPT.is_file())
        script = _load_wikidata_script()
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            dump = data_dir / "simplewiki-20260901-pages-articles.xml.bz2"
            db_path = data_dir / "simplewiki-20260901-pages-articles-wtp-full.db"
            dump.touch()
            output = StringIO()
            script.DATA_DIR = data_dir
            with (
                patch.object(script, "ensure_wtp_database", return_value=db_path) as ensure,
                redirect_stdout(output),
            ):
                script.main([dump.name])

            ensure.assert_called_once_with(
                dump.resolve(), {"lang_code": "en", "project": "wikipedia"}
            )
            self.assertEqual(f"WTP database: {db_path}\n", output.getvalue())

    def test_uncompressed_xml_is_rejected_before_build(self) -> None:
        """验证：testuncompressedxml为rejected之前构建的预期行为。"""
        with self.assertRaisesRegex(ValueError, "xml.bz2"):
            wtp_database_path_for_dump(Path("simplewiki-20260801-pages-articles.xml"))

    def test_build_closes_wtp_connection_when_dump_import_fails(self) -> None:
        """验证：test构建closeswtp连接when转储importfails的预期行为。"""
        with tempfile.TemporaryDirectory() as tmp:
            dump = Path(tmp) / "simplewiki-20260801-pages-articles.xml.bz2"
            db_path = Path(tmp) / "simplewiki-20260801-pages-articles-wtp-full.db"
            dump.touch()
            wtp = Mock(spec=["close_db_conn"])
            created_paths: list[Path] = []

            def create_wtp(*_args: object, db_path: Path, **_kwargs: object) -> Mock:
                """执行创建wtp的处理逻辑。"""
                created_paths.append(db_path)
                return wtp

            def fail_import(*_args: object, **_kwargs: object) -> None:
                """执行失败import的处理逻辑。"""
                created_paths[0].touch()
                raise RuntimeError("import failed")

            with (
                patch("wikitextprocessor.Wtp", side_effect=create_wtp),
                patch(
                    "wikitextprocessor.dumpparser.process_dump",
                    side_effect=fail_import,
                ),
                self.assertRaisesRegex(RuntimeError, "import failed"),
            ):
                build_wtp_database(dump, db_path, namespace_ids={0, 10, 828})

            wtp.close_db_conn.assert_called_once_with()
            self.assertFalse(db_path.exists())
            self.assertFalse(created_paths[0].exists())

    def test_missing_matching_database_is_built_from_dump(self) -> None:
        """验证：test缺失matching数据库为builtfrom转储的预期行为。"""
        with tempfile.TemporaryDirectory() as tmp:
            dump = Path(tmp) / "simplewiki-20260801-pages-articles.xml.bz2"
            dump.touch()
            expected = (Path(tmp) / "simplewiki-20260801-pages-articles-wtp-full.db").resolve()
            captured: dict[str, object] = {}

            def build(path: Path, db_path: Path, **kwargs: object) -> None:
                """执行构建的处理逻辑。"""
                captured.update(path=path, db_path=db_path, **kwargs)
                db_path.touch()

            with patch("wtp_database.build_wtp_database", side_effect=build):
                result = ensure_wtp_database(
                    dump,
                    {"lang_code": "en", "project": "wikipedia"},
                )

            self.assertEqual(expected, result)
            self.assertTrue(result.is_file())
            self.assertEqual(dump.resolve(), captured["path"])
            self.assertEqual(expected, captured["db_path"])
            self.assertEqual({0, 10, 828}, captured["namespace_ids"])

    def test_existing_matching_database_is_reused(self) -> None:
        """验证：test已有matching数据库为reused的预期行为。"""
        with tempfile.TemporaryDirectory() as tmp:
            dump = Path(tmp) / "simplewiki-20260801-pages-articles.xml.bz2"
            dump.touch()
            db_path = wtp_database_path_for_dump(dump.resolve())
            db_path.touch()

            with patch("wtp_database.build_wtp_database") as build:
                result = ensure_wtp_database(dump, {})

            self.assertEqual(db_path, result)
            build.assert_not_called()

    def test_legacy_configured_database_path_is_ignored(self) -> None:
        """验证：test旧版configured数据库路径为ignored的预期行为。"""
        with tempfile.TemporaryDirectory() as tmp:
            dump = Path(tmp) / "simplewiki-20260801-pages-articles.xml.bz2"
            dump.touch()

            expected = wtp_database_path_for_dump(dump.resolve())

            def build(path: Path, db_path: Path, **kwargs: object) -> None:
                """执行构建的处理逻辑。"""
                self.assertEqual(dump.resolve(), path)
                self.assertEqual(expected, db_path)
                db_path.touch()

            with patch("wtp_database.build_wtp_database", side_effect=build):
                result = ensure_wtp_database(dump, {"db_path": str(Path(tmp) / "other.db")})

            self.assertEqual(expected, result)


if __name__ == "__main__":
    unittest.main()
