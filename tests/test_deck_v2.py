"""Design system v2 (editorial / ember themes, alternating palettes, eyebrow + source lines, drawn cover art, text beside a
table or chart, stacked share chart, `|` bullets) and the research budget for building a deck."""
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pptx import Presentation

from core import deck_research
from core.doc_ops import pptx_ops
from core.doc_ops.pptx_ops import lint
from core.doc_ops.pptx_ops import outline as O

MD = """# Dhaka: The Deep Dive
theme: editorial
subtitle: A deep dive
eyebrow: Capital of Bangladesh
art: arches
---
## Dhaka in five numbers
eyebrow: At a glance
layout: stats
source: UN WUP 2024; census 2022
- 36.6M | Metro population
- 1610 | Year of the charter
- 306 km2 | Area
- 33,600 | People per km2
- 46% | Share of GDP
---
## A city built on rivers
eyebrow: Setting
layout: textside
source: BMD
- Dhaka sits on the lower Ganges Delta.
- Six rivers surround it.
| Month | Mean C | Rain |
|---|---|---|
| Jan | 18.4 | 7 |
| Jul | 28.9 | 391 |
---
## Engine
eyebrow: Economy
chart: stacked | unit=%
source: DCCI 2025
- Garments: 58.6
- Food: 13.4
- Textiles: 9.3
---
## Dates
layout: timeline
- 21 Feb 1952 | Students march
- 16 Dec 1971 | Surrender
---
## Thank you
"""


class Outline(unittest.TestCase):
    def setUp(self):
        self.deck = O.parse_outline(MD)
        self.s = self.deck["slides"]

    def test_new_fields(self):
        self.assertEqual(self.s[0]["art"], "arches")
        self.assertEqual(self.s[0]["eyebrow"], "CAPITAL OF BANGLADESH")
        self.assertEqual(self.s[1]["eyebrow"], "AT A GLANCE")
        self.assertEqual(self.s[1]["source"], "UN WUP 2024; census 2022")

    def test_pipe_bullets_split_into_number_and_label(self):
        self.assertEqual(O.stat_split("306 km2 | Area"), ("306 km2", "Area"))
        self.assertEqual(O.label_split("21 Feb 1952 | Students march"), ("21 Feb 1952", "Students march"))
        self.assertEqual([k["kind"] for k in self.s][1], "stats")

    def test_five_numbers_make_a_stats_slide(self):
        self.assertEqual(len(O.groups(self.s[1]["bullets"])), 5)
        self.assertEqual(self.s[1]["kind"], "stats")

    def test_text_beside_a_table(self):
        self.assertEqual(self.s[2]["kind"], "textside")
        self.assertEqual(len(self.s[2]["table"]), 3)

    def test_text_beside_a_chart(self):
        md = "# D\nsubtitle: s\n---\n## T\nlayout: textside\nchart: line\n- Some text about it\n- 2024: 4\n- 2025: 9\n"
        s = O.parse_outline(md)["slides"][1]
        self.assertEqual(s["kind"], "textside")
        self.assertTrue(s["chart_data"])

    def test_textside_without_data_is_plain_bullets(self):
        md = "# D\nsubtitle: s\n---\n## T\nlayout: textside\n- one\n- two\n- three\n"
        self.assertEqual(O.parse_outline(md)["slides"][1]["kind"], "bullets")

    def test_stacked_share_chart(self):
        self.assertEqual(self.s[3]["chart_data"]["kind"], "share")
        self.assertEqual(self.s[3]["chart_data"]["labels"], ["Garments", "Food", "Textiles"])

    def test_timeline_from_pipe_bullets(self):
        self.assertEqual(self.s[4]["kind"], "timeline")

    def test_unknown_art_is_ignored(self):
        self.assertEqual(O.parse_outline("# D\nart: volcano\nsubtitle: s\n")["slides"][0]["art"], "")

    def test_themes_exist(self):
        for name in ("editorial", "ember"):
            self.assertIn(name, __import__("core.doc_ops.pptx_ops.theme", fromlist=["x"]).THEMES)
        self.assertEqual(O.resolve_theme(O.parse_outline("# D\ntheme: cream and teal\nsubtitle: s\n"))["name"], "editorial")


class Rendering(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = pptx_ops.create(MD)
        cls.prs = Presentation(io.BytesIO(cls.data))

    def _bg(self, slide):
        return str(slide.background.fill.fore_color.rgb)

    def test_content_slides_alternate_between_the_two_palettes(self):
        bgs = [self._bg(self.prs.slides[i]) for i in (1, 2, 3, 4)]
        self.assertEqual(bgs, ["F4EFE4", "10302D", "F4EFE4", "10302D"])

    def test_cover_is_the_deep_colour_with_drawn_art(self):
        cover = self.prs.slides[0]
        self.assertEqual(self._bg(cover), "10302D")
        art = [sh for sh in cover.shapes if sh.name == "deco-art"]
        self.assertGreater(len(art), 8)

    def test_every_art_motif_draws_on_any_theme(self):
        for kind in O.ART_KINDS:
            if kind == "none":
                continue
            md = f"# D\ntheme: midnight\nart: {kind}\nsubtitle: s\n---\n## A\n- x\n- y\n"
            prs = Presentation(io.BytesIO(pptx_ops.create(md)))
            self.assertTrue(any(sh.name.startswith("deco") for sh in prs.slides[0].shapes), kind)

    def test_eyebrow_label_and_source_footer(self):
        sl = self.prs.slides[1]
        eb = next(sh for sh in sl.shapes if sh.name == "deco-eyebrow")
        self.assertEqual(eb.text_frame.text, "AT A GLANCE")
        foot = next(sh for sh in sl.placeholders if sh.name == "deco-footer")
        self.assertEqual(foot.text_frame.text, "Source: UN WUP 2024; census 2022")

    def test_a_slide_without_a_source_keeps_the_deck_title_in_the_footer(self):
        sl = self.prs.slides[4]
        foot = next(sh for sh in sl.placeholders if sh.name == "deco-footer")
        self.assertIn("Dhaka", foot.text_frame.text)

    def test_five_stat_cards(self):
        sl = self.prs.slides[1]
        self.assertEqual(sum(1 for sh in sl.shapes if sh.name.startswith("Stat ")), 5)

    def test_numbers_share_one_size(self):
        sizes = set()
        for sh in self.prs.slides[1].shapes:
            if sh.name.startswith("Stat ") and sh.has_text_frame:
                sizes.add(sh.text_frame.paragraphs[0].runs[0].font.size.pt)
        self.assertEqual(len(sizes), 1)

    def test_text_left_table_right(self):
        sl = self.prs.slides[2]
        table = next(sh for sh in sl.shapes if sh.has_table)
        body = next(sh for sh in sl.placeholders if sh.placeholder_format.idx == 1)
        self.assertLess(body.left + body.width, table.left)

    def test_share_chart_is_one_stacked_bar(self):
        chart = next(sh.chart for sh in self.prs.slides[3].shapes if sh.has_chart)
        self.assertEqual(len(list(chart.plots[0].categories)), 1)
        self.assertEqual(len(list(chart.plots[0].series)), 3)

    def test_the_whole_deck_is_lint_clean(self):
        self.assertEqual([f["message"] for f in lint.lint_deck(self.data)], [])

    def test_ember_is_clean_too(self):
        data = pptx_ops.create(MD.replace("theme: editorial", "theme: ember"))
        self.assertEqual([f["message"] for f in lint.lint_deck(data)], [])


class Citations(unittest.TestCase):
    MD = ("# D\nsubtitle: s\n---\n## At a Glance\n"
          "- 25.4 million people in the metro area [Wikipedia](https://en.wikipedia.org/wiki/Dhaka) / "
          "[InsideBangladesh](httpshttps://insinside-bangladesh.com/dd/d/dhaka-city)\n"
          "- Contributes 36% of GDP https://example.org/x\n")

    def test_links_leave_the_text_and_become_the_source(self):
        s = O.parse_outline(self.MD)["slides"][1]
        self.assertEqual(s["bullets"], ["25.4 million people in the metro area", "Contributes 36% of GDP"])
        self.assertEqual(s["source"], "Wikipedia; InsideBangladesh")

    def test_an_explicit_source_line_wins(self):
        md = self.MD + "source: UN WUP 2024\n"
        self.assertEqual(O.parse_outline(md)["slides"][1]["source"], "UN WUP 2024")

    def test_image_urls_in_field_lines_are_kept(self):
        md = "# D\nsubtitle: s\n---\n## A\nimage: https://example.org/pic.jpg\n- one\n- two\n"
        self.assertEqual(O.parse_outline(md)["slides"][1]["image"]["url"], "https://example.org/pic.jpg")

    def test_plain_bracket_text_is_untouched(self):
        md = "# D\nsubtitle: s\n---\n## A\n- Use [brackets] and (parentheses) freely\n- Another line here\n"
        self.assertIn("[brackets]", " ".join(O.parse_outline(md)["slides"][1]["bullets"]))


class TypeSize(unittest.TestCase):
    def test_a_few_short_lines_can_be_big_and_dense_text_is_smaller(self):
        from core.doc_ops.pptx_ops import design as D
        short = ["Fast", "Cheap", "Safe"]
        dense = ["25.4 million people live in the metro area and it ranks among the fastest growing megacities in the world"] * 3
        very = dense * 2
        self.assertEqual(D.bullet_base(short), 26)
        self.assertLessEqual(D.bullet_base(dense), 22)
        self.assertEqual(D.bullet_base(very), 19)
        self.assertEqual(D.bullet_base(dense, True), D.bullet_base(dense) - 2)

    def test_body_text_on_a_dense_slide_stays_readable_not_poster_sized(self):
        md = ("# D\nsubtitle: s\n---\n## At a Glance\n"
              "- 25.4 million people live in the metro area, one of the world's fastest-growing megacities today\n"
              "- Contributes over 36% of Bangladesh's GDP, making it the nation's economic engine for decades\n"
              "- Founded as a Mughal capital in 1610 and renamed Jahangirnagar after the Emperor Jahangir\n")
        prs = Presentation(io.BytesIO(pptx_ops.create(md)))
        sizes = {r.font.size.pt for sh in prs.slides[1].placeholders if sh.placeholder_format.idx == 1
                 for p in sh.text_frame.paragraphs for r in p.runs}
        self.assertTrue(sizes and max(sizes) <= 22, sizes)


class ResearchBudget(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(deck_research.calls({}), 10)
        self.assertEqual(deck_research.calls({"chat": {"deck_research_calls": 6}}), 6)
        self.assertEqual(deck_research.calls({"chat": {"deck_research_calls": "x"}}), 10)
        self.assertEqual(deck_research.calls({"chat": {"deck_research_calls": 999}}), 40)

    def test_effort_is_raised_to_the_floor_never_lowered(self):
        self.assertEqual(deck_research.effort_for("low", {}), "medium")
        self.assertEqual(deck_research.effort_for("high", {}), "high")
        self.assertEqual(deck_research.effort_for("low", {"chat": {"deck_effort": "low"}}), "low")
        self.assertEqual(deck_research.effort_for(None, {}), None)

    def test_build_phase_gets_the_bigger_budget(self):
        p = deck_research.web_plan({}, "build", True, "none", False, True, 4)
        self.assertEqual(p, {"has_web": True, "lean": False, "budget": 10})

    def test_a_bigger_existing_budget_is_kept(self):
        self.assertEqual(deck_research.web_plan({}, "build", True, "none", True, False, 20)["budget"], 20)

    def test_nothing_changes_outside_the_build_phase_or_without_the_web_switch(self):
        keep = {"has_web": False, "lean": False, "budget": 0}
        self.assertEqual(deck_research.web_plan({}, "ask", True, "none", False, False, 0), keep)
        self.assertEqual(deck_research.web_plan({}, "", True, "none", False, False, 0), keep)
        self.assertEqual(deck_research.web_plan({}, "build", False, "none", False, False, 0), keep)

    def test_a_fully_covered_topic_stays_off_the_web(self):
        keep = {"has_web": False, "lean": True, "budget": 0}
        self.assertEqual(deck_research.web_plan({}, "build", True, "full", False, True, 0), keep)

    def test_switched_off(self):
        keep = {"has_web": False, "lean": False, "budget": 0}
        self.assertEqual(deck_research.web_plan({"chat": {"deck_research_calls": 0}}, "build", True, "none", False, False, 0), keep)

    def test_tool_rounds_cover_research_and_build(self):
        self.assertEqual(deck_research.turns_for(14, 12), 26)
        self.assertEqual(deck_research.turns_for(10, 12), 22)
        self.assertEqual(deck_research.turns_for(2, 30), 30)

    def test_building_has_no_repeat_penalty_and_a_token_cap(self):
        self.assertEqual(deck_research.build_limits({}, "build"), {"repeat_penalty": 1.0, "max_tokens": 8000})
        self.assertEqual(deck_research.build_limits({}, "ask"), {"repeat_penalty": None, "max_tokens": None})
        self.assertEqual(deck_research.build_limits({}, ""), {"repeat_penalty": None, "max_tokens": None})
        cfg = {"chat": {"deck_repeat_penalty": 1.05, "deck_max_tokens": 4000}}
        self.assertEqual(deck_research.build_limits(cfg, "build"), {"repeat_penalty": 1.05, "max_tokens": 4000})
        off = {"chat": {"deck_repeat_penalty": 0, "deck_max_tokens": 0}}
        self.assertEqual(deck_research.build_limits(off, "build"), {"repeat_penalty": None, "max_tokens": None})

    def test_unlimited_requests_are_capped_and_small_ones_kept(self):
        self.assertEqual(deck_research.clamp_tokens(-1, 8000), 8000)
        self.assertEqual(deck_research.clamp_tokens(None, 8000), 8000)
        self.assertEqual(deck_research.clamp_tokens(0, 8000), 8000)
        self.assertEqual(deck_research.clamp_tokens(32000, 8000), 8000)
        self.assertEqual(deck_research.clamp_tokens(2000, 8000), 2000)
        self.assertEqual(deck_research.clamp_tokens(-1, None), -1)

    def test_web_results_are_trimmed_harder_while_building(self):
        self.assertEqual(deck_research.web_cap("build"), (1, 4500))
        self.assertEqual(deck_research.web_cap(""), (6, 6000))

    def test_the_chat_route_uses_it(self):
        src = (Path(__file__).resolve().parents[1] / "routes" / "chat" / "run.py").read_text(encoding="utf-8")
        for needle in ("deck_research.effort_for", "deck_research.web_plan", "deck_research.turns_for",
                       "deck_research.build_limits", "deck_research.clamp_tokens", "deck_research.web_cap"):
            self.assertIn(needle, src)


if __name__ == "__main__":
    unittest.main()
