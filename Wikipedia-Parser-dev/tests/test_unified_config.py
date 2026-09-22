"""单个流水线配置文件的行为测试。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parents[1] / "pipeline"
sys.path.insert(0, str(PIPELINE_DIR))

import pipeline_config  # noqa: E402
from pipeline_config import load_pipeline_config, resolve_dump_file  # noqa: E402
from wikipedia_parser import build_parser  # noqa: E402


class UnifiedConfigTest(unittest.TestCase):
    def test_config_option_is_required(self) -> None:
        """The parser must have one mandatory unified configuration file."""
        args = build_parser().parse_args(["--config", "config.json"])

        self.assertEqual(Path("config.json"), args.config)
        self.assertEqual("wiki_component", args.table_prefix)
        self.assertEqual("wiki_processed", args.processed_table)
        self.assertEqual("wiki_sections", args.sections_table)
        self.assertEqual("wiki_paragraph", args.paragraphs_table)
        self.assertEqual("wiki_wtp_intermediate", args.wtp_intermediate_table)
        self.assertEqual("wiki_sentence", args.sentences_table)
        self.assertEqual("wiki_parse_checkpoint", args.checkpoint_table)
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
                source_table="latest",
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

    def test_xml_dump_suffixes_every_output_table_with_the_dump_date(self) -> None:
        """Every XML output table ends with the dump's eight-digit date."""
        table_names_for_dump = getattr(
            pipeline_config, "output_table_names_for_dump", None
        )
        self.assertIsNotNone(table_names_for_dump)
        assert table_names_for_dump is not None
        table_names = table_names_for_dump(
            Path("simplewiki-20260801-pages-articles.xml.bz2"),
            raw_table="latest",
            processed_table="processed",
            component_prefix="component",
            sections_table="sections",
            paragraphs_table="paragraph",
            wtp_intermediate_table="wtp_intermediate",
            sentences_table="sentence",
            checkpoint_table="parse_checkpoint",
        )

        suffix = "_[20260801]"
        self.assertEqual("wiki_latest" + suffix, table_names.raw)
        self.assertEqual("wiki_processed" + suffix, table_names.processed)
        self.assertEqual("wiki_component_infobox" + suffix, table_names.component_table_name("infobox"))
        self.assertEqual("wiki_component_table" + suffix, table_names.component_table_name("table"))
        self.assertEqual("wiki_component_wikilinks" + suffix, table_names.component_table_name("wikilinks"))
        self.assertEqual("wiki_component_external_links" + suffix, table_names.component_table_name("external_links"))
        self.assertEqual("wiki_component_file" + suffix, table_names.component_table_name("file"))
        self.assertEqual("wiki_component_image" + suffix, table_names.component_table_name("image"))
        self.assertEqual("wiki_component_ref" + suffix, table_names.component_table_name("ref"))
        self.assertEqual("wiki_sections" + suffix, table_names.sections)
        self.assertEqual("wiki_paragraph" + suffix, table_names.paragraphs)
        self.assertEqual("wiki_wtp_intermediate" + suffix, table_names.wtp_intermediate)
        self.assertEqual("wiki_sentence" + suffix, table_names.sentences)
        self.assertEqual("wiki_parse_checkpoint" + suffix, table_names.checkpoint)

    def test_xml_dump_without_an_eight_digit_date_is_rejected(self) -> None:
        """A generic XML stem must not silently route data into an unsuffixed table."""
        table_names_for_dump = getattr(
            pipeline_config, "output_table_names_for_dump", None
        )
        self.assertIsNotNone(table_names_for_dump)
        assert table_names_for_dump is not None
        with self.assertRaisesRegex(ValueError, "eight-digit date"):
            table_names_for_dump(
                Path("simplewiki-latest-pages-articles.xml.bz2"),
                raw_table="latest",
                processed_table="processed",
                component_prefix="component",
                sections_table="sections",
                paragraphs_table="paragraph",
                wtp_intermediate_table="wtp_intermediate",
                sentences_table="sentence",
                checkpoint_table="parse_checkpoint",
            )


if __name__ == "__main__":
    unittest.main()
