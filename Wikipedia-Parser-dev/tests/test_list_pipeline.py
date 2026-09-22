"""回归测试覆盖通过 WTP 段落流水线的列表。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parents[1] / "pipeline"
sys.path.insert(0, str(PIPELINE_DIR))

from engine import COMPONENT_TYPES, extract_and_templatize  # noqa: E402
from paragraph_extractor import Paragraph  # noqa: E402
from sentence_extractor import extract_sentences  # noqa: E402


class ListPipelineTest(unittest.TestCase):
    def test_lists_are_not_components_and_reach_sentence_processing(self) -> None:
        source = "Intro:\n* First item.\n** Child item.\n* Second [[Target]].\n"

        wtp_input, components = extract_and_templatize(source, page_id=99)

        self.assertNotIn("list", COMPONENT_TYPES)
        self.assertEqual(source, wtp_input)
        self.assertNotIn("list", components)
        self.assertEqual([], components["wikilinks"])

        sentences = extract_sentences(
            [Paragraph(section_no=1, paragraph_no=1, toc=None, text=wtp_input)],
            page_id=99,
        )

        self.assertEqual(1, len(sentences))
        self.assertIn("First item", sentences[0].text)
        self.assertIn("Child item", sentences[0].text)
        self.assertIn("[[Target↓Target]]", sentences[0].text)


if __name__ == "__main__":
    unittest.main()
