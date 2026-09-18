"""Sections that must remain paragraph-only and bypass WTP."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
PIPELINE_DIR = PROJECT_DIR / "pipeline"
sys.path.insert(0, str(PIPELINE_DIR))

from engine import (  # noqa: E402
    WtpIntermediateWriter,
    SentencesWriter,
    build_bundle,
    should_skip_wtp_for_toc,
)
from paragraph_extractor import Paragraph  # noqa: E402


class SkipWtpSectionsTest(unittest.TestCase):
    def test_target_toc_roots_are_case_insensitive_and_include_subsections(self) -> None:
        """Changing a target title's case or adding ':child' must not invoke WTP."""
        for toc in (
            "REFERENCES:Books",
            "Other Websites",
            "related PAGES:Directory",
            "MORE READING",
            "Category:Topics",
            "EXTERNAL links",
            "footnotes:Notes",
        ):
            with self.subTest(toc=toc):
                self.assertTrue(should_skip_wtp_for_toc(toc))
        self.assertFalse(should_skip_wtp_for_toc("History:Background"))

    def test_target_paragraphs_bypass_wtp_sentences_and_intermediate_output(self) -> None:
        """Reference-path paragraphs remain in paragraph output but never reach WTP."""
        row = {
            "revision_id": 1,
            "page_id": 2,
            "page_title": "Example",
            "namespace": 0,
            "model": "wikitext",
            "content": """
==rEfErEnCeS==
===Books===
Reference paragraph.

==OTHER WEBSITES==
External paragraph.

==History==
Ordinary paragraph.
""",
        }

        with (
            patch("engine.begin_page") as begin_page,
            patch("engine.analyze_wikitext", return_value=(0, None)) as analyze,
            patch(
                "engine.expand_to_text",
                side_effect=lambda text, _timeout: ("expanded: " + text, text, None),
            ) as expand,
            patch("engine.extract_sentences", return_value=[]) as extract_sentences,
        ):
            bundle, error, stats = build_bundle(row, expand_timeout=15.0)

        self.assertIsNone(error)
        self.assertEqual(1, stats.total)
        begin_page.assert_called_once_with("Example")
        self.assertEqual(1, analyze.call_count)
        self.assertEqual(1, expand.call_count)
        self.assertEqual("Ordinary paragraph.", expand.call_args.args[0])
        self.assertEqual(
            ["History"],
            [paragraph.toc for paragraph in extract_sentences.call_args.args[0]],
        )

        paragraphs = {paragraph.toc: paragraph for paragraph in bundle["paragraphs"]}
        self.assertTrue(paragraphs["rEfErEnCeS:Books"].wtp_skipped)
        self.assertTrue(paragraphs["OTHER WEBSITES"].wtp_skipped)
        self.assertEqual("Reference paragraph.", paragraphs["rEfErEnCeS:Books"].text)
        self.assertEqual("expanded: Ordinary paragraph.", paragraphs["History"].text)

        writer = object.__new__(WtpIntermediateWriter)
        writer._buffer = []
        writer._pending_revision_ids = set()
        writer.batch_size = 99
        writer.add_rows([bundle])
        self.assertEqual(1, len(writer._buffer))
        self.assertEqual("History", writer._buffer[0][5])

    def test_empty_skip_only_output_deletes_stale_wtp_and_sentence_rows(self) -> None:
        """Reprocessing a skip-only article must not leave old derived rows behind."""
        bundle = {
            "revision_id": 41,
            "page_id": 4,
            "page_title": "References only",
            "paragraphs": [
                _skip_only_paragraph(),
            ],
            "sentences": [],
        }

        for writer_type in (WtpIntermediateWriter, SentencesWriter):
            with self.subTest(writer=writer_type.__name__):
                writer = object.__new__(writer_type)
                writer._buffer = []
                writer._pending_revision_ids = set()
                writer.batch_size = 99
                writer.qualified = "[dbo].[test]"
                writer._conn = MagicMock()
                writer.written = 0
                with (
                    patch("engine._delete_by_keys") as delete,
                    patch("engine._insert_multirow") as insert,
                ):
                    writer.add_rows([bundle])
                    writer.flush()

                delete.assert_called_once_with(
                    writer._conn, "[dbo].[test]", "revision_id", {41}
                )
                insert.assert_not_called()


def _skip_only_paragraph() -> Paragraph:
    return Paragraph(
        section_no=1,
        paragraph_no=1,
        toc="References",
        text="Reference paragraph.",
        raw_wikitext="Reference paragraph.",
        wtp_skipped=True,
    )


if __name__ == "__main__":
    unittest.main()
