"""Regression coverage for the lightweight per-paragraph MWP validation."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parents[1] / "pipeline"
sys.path.insert(0, str(PIPELINE_DIR))

import wtp_integration  # noqa: E402


class MwpAnalysisTest(unittest.TestCase):
    def test_validation_does_not_walk_templates(self) -> None:
        class Parsed:
            def filter_templates(self, recursive: bool = True) -> list[object]:
                raise AssertionError("template traversal must not be called")

        original_parse = wtp_integration.mwparserfromhell.parse
        try:
            wtp_integration.mwparserfromhell.parse = lambda text: Parsed()
            self.assertEqual((0, None), wtp_integration.analyze_wikitext("{{T}}"))
        finally:
            wtp_integration.mwparserfromhell.parse = original_parse


if __name__ == "__main__":
    unittest.main()
