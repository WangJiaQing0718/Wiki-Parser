"""Behavior tests for the single pipeline configuration file."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parents[1] / "pipeline"
sys.path.insert(0, str(PIPELINE_DIR))

from pipeline_config import load_pipeline_config, resolve_dump_file  # noqa: E402
from wikipedia_parser import build_parser  # noqa: E402


class UnifiedConfigTest(unittest.TestCase):
    def test_config_option_is_required(self) -> None:
        """The parser must have one mandatory unified configuration file."""
        args = build_parser().parse_args(["--config", "config.json"])

        self.assertEqual(Path("config.json"), args.config)
        with self.assertRaises(SystemExit):
            build_parser().parse_args([])
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["--db-config", "db.json"])

    def test_config_supplies_sqlserver_wtp_and_default_dump(self) -> None:
        """A unified config must resolve all three assets relative to itself."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_dir = root / "data"
            db_dir.mkdir()
            wtp_db = db_dir / "simplewiki.db"
            dump = db_dir / "simplewiki.xml.bz2"
            wtp_db.touch()
            dump.touch()
            config_file = root / "config.json"
            config_file.write_text(
                json.dumps(
                    {
                        "source": {"dump_file": "data/simplewiki.xml.bz2"},
                        "sqlserver": {
                            "host": "db.example.test",
                            "user": "reader",
                            "password": "secret",
                            "database": "wiki",
                        },
                        "wtp": {"db_path": "data/simplewiki.db"},
                    }
                ),
                encoding="utf-8",
            )

            config = load_pipeline_config(config_file)

            self.assertEqual("db.example.test", config.sqlserver["host"])
            self.assertEqual(str(wtp_db.resolve()), config.wtp["db_path"])
            self.assertEqual(dump.resolve(), config.dump_file)

    def test_cli_dump_overrides_configured_dump_but_source_table_disables_xml(self) -> None:
        """A one-off dump path must win; SQL input must not also open configured XML."""
        configured = Path("configured.xml.bz2")
        override = Path("override.xml.bz2")

        self.assertEqual(
            override,
            resolve_dump_file(
                cli_dump_file=override,
                source_table=None,
                configured_dump_file=configured,
            ),
        )
        self.assertIsNone(
            resolve_dump_file(
                cli_dump_file=None,
                source_table="simplewiki_latest",
                configured_dump_file=configured,
            )
        )
        self.assertEqual(
            configured,
            resolve_dump_file(
                cli_dump_file=None,
                source_table=None,
                configured_dump_file=configured,
            ),
        )


if __name__ == "__main__":
    unittest.main()
