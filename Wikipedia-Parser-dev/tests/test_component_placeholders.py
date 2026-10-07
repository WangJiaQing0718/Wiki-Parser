"""回归测试覆盖组件占位符序列化。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parents[1] / "pipeline"
sys.path.insert(0, str(PIPELINE_DIR))

from component_extractor import extract_and_templatize  # noqa: E402
from sentence_extractor import _remove_ref_placeholders  # noqa: E402


class ComponentPlaceholderTest(unittest.TestCase):
    def test_extracts_each_template_in_a_template_only_paragraph(self) -> None:
        """验证仅包含模板的段落中，每个模板都会被提取。"""
        merge = "{{Merge to|China|date=July 2026}}"
        about = "{{About|the People's Republic of China|Taiwan}}"
        infobox = "{{Infobox country\n| native_name = Zhongguo\n\n| common_name = China\n}}"
        source = f"{merge}\n{about}{infobox}\n\nThe '''People's Republic of China''' is in East Asia."

        text_process, components = extract_and_templatize(source, page_id=42)

        self.assertEqual(
            [
                ("independent_template-42-0001", merge),
                ("independent_template-42-0002", about),
            ],
            components["independent_template"],
        )
        self.assertEqual(
            [("infobox-42-0001", infobox)],
            components["infobox"],
        )
        self.assertIn("The '''People's Republic of China''' is in East Asia.", text_process)

    def test_extracts_multiline_top_level_template_with_nested_templates(self) -> None:
        """验证多行顶层模板及其嵌套模板会被正确提取。"""
        taxobox = (
            "{{automatic taxobox\n"
            "| name = Life\n"
            "| fossil_range = {{Long fossil range|3770|0|earliest=4280}} present\n"
            "| subdivision = Life on Earth:\n"
            "* [[Cellular life]]\n"
            "}}"
        )
        source = f"{{{{other uses}}}}\n{taxobox}\n\n'''Life''' is matter."

        text_process, components = extract_and_templatize(source, page_id=42)

        self.assertEqual(
            [
                ("independent_template-42-0001", "{{other uses}}"),
                ("independent_template-42-0002", taxobox),
            ],
            components["independent_template"],
        )
        self.assertIn("independent_template-42-0002", text_process)
        self.assertNotIn("Long fossil range", text_process)

    def test_extracts_only_outermost_paired_template_blocks(self) -> None:
        """验证只会提取最外层的成对模板区块。"""
        succession_block = (
            "{{S-start}}\n\n"
            "{{succession box|title=[[British Ambassador to France]]|years=1954-1960}}\n"
            "{{s-off}}\n"
            "{{succession box|title=[[UN Secretary-General]]|years=1945-1946}}\n\n"
            "{{End}}"
        )
        chart_block = (
            "{{chart top|Henry Fonda family tree}}\n\n"
            "{{Tree chart/start|align=center}}\n"
            "{{Tree chart|P10|v|P11}}\n"
            "{{Tree chart/end}}\n"
            "{{chart bottom}}"
        )
        medal_block = (
            "{{MedalTableTop\n| title = International and National Medals\n}}\n\n"
            "{{MedalSport | Men's Powerlifting}}\n"
            "{{MedalGold | 2019 | National Bench Press | Iran}}\n\n"
            "{{MedalTableBottom}}"
        )
        source = f"Before\n{succession_block}\nAfter\n{chart_block}\n{medal_block}\n{{{{Unclosed start}}}}\ntext"

        text_process, components = extract_and_templatize(source, page_id=42)

        self.assertEqual(
            [
                ("independent_template-42-0001", succession_block),
                ("independent_template-42-0002", chart_block),
                ("independent_template-42-0003", medal_block),
            ],
            components["independent_template"],
        )
        self.assertNotIn("Tree chart/start", text_process)
        self.assertIn("{{Unclosed start}}", text_process)

    def test_extracts_complete_templates_that_occupy_a_line(self) -> None:
        """验证独占一行的完整模板会被提取。"""
        source = (
            "{{Merge to|China||discuss=Talk:China#Merging discussion (2026)|date=July 2026}}\n"
            "{{About|the People's Republic of China|the Republic of China}}\n"
            "[[File:Graph.png|thumb|Growth.{{seealso|GDP}}]]\n"
            "Article text {{citation needed}}.\n"
            "{{multiline|\n"
            "value=not an independent line template\n"
            "}}\n"
        )

        text_process, components = extract_and_templatize(source, page_id=42)

        self.assertEqual(
            [
                (
                    "independent_template-42-0001",
                    "{{Merge to|China||discuss=Talk:China#Merging discussion (2026)|date=July 2026}}",
                ),
                (
                    "independent_template-42-0002",
                    "{{About|the People's Republic of China|the Republic of China}}",
                ),
                (
                    "independent_template-42-0003",
                    "{{multiline|\nvalue=not an independent line template\n}}",
                ),
            ],
            components["independent_template"],
        )
        self.assertIn("independent_template-42-0001", text_process)
        self.assertIn("independent_template-42-0002", text_process)
        self.assertEqual(
            [("file-42-0001", "[[File:Graph.png|thumb|Growth.{{seealso|GDP}}]]")],
            components["file"],
        )
        self.assertIn("Article text {{citation needed}}.", text_process)
        self.assertIn("independent_template-42-0003", text_process)

    def test_extracts_adjacent_templates_that_together_occupy_a_line(self) -> None:
        """相邻模板合起来独占一行时，逐个提取且不受后续文件内容影响。"""
        more_sources = "{{More sources|date=March 2020}}"
        cleanup = "{{Cleanup|date=March 2025}}"
        infobox = "{{Infobox software license\n| name = GFDL\n}}"
        file_link = "[[File:Heckert GNU white.svg|thumb|GNU logo]]"
        source = (
            f"{more_sources}{cleanup}\n"
            f"{infobox}\n"
            f"{file_link}\n"
            "The GNU Free Documentation License is a copyleft license."
        )

        text_process, components = extract_and_templatize(source, page_id=42)

        self.assertEqual(
            [
                ("independent_template-42-0001", more_sources),
                ("independent_template-42-0002", cleanup),
            ],
            components["independent_template"],
        )
        self.assertEqual(
            [("infobox-42-0001", infobox)],
            components["infobox"],
        )
        self.assertEqual(
            [("file-42-0001", file_link)],
            components["file"],
        )
        self.assertIn("The GNU Free Documentation License is a copyleft license.", text_process)

    def test_extracted_components_use_club_delimited_placeholders(self) -> None:
        """验证提取出的组件使用带 ♣ 分隔符的占位符。"""
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
        """验证句子处理阶段会移除带 ♣ 分隔符的 ref 占位符。"""
        text = "Before ♣  ♣  ♣  ref-42-0001♣  ♣  ♣ after"

        self.assertEqual("Before  after", _remove_ref_placeholders(text))


if __name__ == "__main__":
    unittest.main()
