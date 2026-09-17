"""Regression coverage for component placeholder serialization."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parents[1] / "pipeline"
sys.path.insert(0, str(PIPELINE_DIR))

from engine import extract_and_templatize  # noqa: E402
from sentence_extractor import _remove_ref_placeholders  # noqa: E402


class ComponentPlaceholderTest(unittest.TestCase):
    def test_extracted_components_use_club_delimited_placeholders(self) -> None:
        cases = (
            ("{{Infobox person}}", "infobox"),
            ("{|\n| cell\n|}", "table"),
            ("<ref>source</ref>", "ref"),
            ("[[File:example.png]]", "file"),
            ("[[Image:example.png]]", "image"),
            ("[https://example.com label]", "external_links"),
        )

        for source, component_type in cases:
            with self.subTest(component_type=component_type):
                text_process, components = extract_and_templatize(source, page_id=42)

                component_id = f"{component_type}-42-0001"
                self.assertIn(
                    f"♣  ♣  ♣  {component_id}♣  ♣  ♣", text_process
                )
                self.assertEqual(component_id, components[component_type][0][0])
                self.assertNotIn("{{" + component_id + "}}", text_process)

    def test_sentence_processing_removes_club_delimited_ref_placeholders(self) -> None:
        text = "Before ♣  ♣  ♣  ref-42-0001♣  ♣  ♣ after"

        self.assertEqual("Before  after", _remove_ref_placeholders(text))


if __name__ == "__main__":
    unittest.main()
