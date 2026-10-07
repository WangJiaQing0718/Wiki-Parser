"""回归测试覆盖句子阶段的 wikilink 格式化。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parents[1] / "pipeline"
sys.path.insert(0, str(PIPELINE_DIR))

from sentence_extractor import _process_wikilinks  # noqa: E402


class SentenceWikilinkTest(unittest.TestCase):
    def test_component_template_fallback_link_uses_normal_wikilink_format(self) -> None:
        """验证组件模板的回退链接使用常规 wikilink 格式。"""
        text = "[[:Template:infobox-600-0001]]"

        self.assertEqual(
            "[[:Template:infobox-600-0001↓:Template:infobox-600-0001]]",
            _process_wikilinks(text),
        )

    def test_article_links_keep_their_sentence_representation(self) -> None:
        """验证文章链接在句子阶段保留原有表示形式。"""
        text = "[[:Taiwan]] [[Taiwan]] [[Communism|Communist]]"

        self.assertEqual(
            "[[:Taiwan\u2193:Taiwan]] [[Taiwan\u2193Taiwan]] "
            "[[Communist\u2193Communism]]",
            _process_wikilinks(text),
        )


if __name__ == "__main__":
    unittest.main()
