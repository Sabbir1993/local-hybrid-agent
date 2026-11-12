"""Deck quality: the KPI-style layouts are lint-clean on every theme (dark and light), and the builder reports what it
changed on its own instead of silently producing a different deck."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.agent_tools  # noqa: F401  (import order: doc tools register through agent_tools)
from core.doc_ops import pptx_ops
from core.doc_ops.pptx_ops import design, lint, notes, theme

DECK = (
    "# Merchant Performance Review\nsubtitle: Q2 summary\ntheme: {theme}\n---\n"
    "## Executive summary\nlayout: kpi-grid\n- 97.4% | ^ 1.2 pts | Payment success rate | tone=green\n"
    "- 4.2M | ^ 18% | Transactions | tone=blue\n- 310 ms | Median response time | tone=navy\n"
    "- 0.08% | ^ 0.02 pts | Dispute rate | tone=amber\ncallout: Volume and reliability both improved.\n---\n"
    "## Channel mix\nlayout: chart-kpis\nchart: bar\n| Channel | Q1 | Q2 |\n|---|---|---|\n| Cards | 120 | 150 |\n"
    "| MFS | 300 | 340 |\n| Net banking | 80 | 95 |\n- +25% | Card volume growth | tone=green\n"
    "- +13% | MFS volume growth | tone=blue\n- -2% | Net banking share | tone=red\n---\n"
    "## Rollout plan\nlayout: roadmap\n- 1 | Pilot | Jan 2026 | Ten merchants onboarded | status=done\n"
    "- 2 | Launch | Apr 2026 | Open to all merchants | status=now\n- 3 | Scale | Sep 2026 | Regional expansion | status=planned\n---\n"
    "## Readiness\nlayout: dimension-table\n| Area | Score | Status |\n|---|---|---|\n| Security | 4.5 | Ready |\n"
    "| Support | 3.1 | Needs work |\nalert: Support staffing must be resolved before launch.\n---\n## Thank you\n")


class KpiLayoutsTests(unittest.TestCase):
    def test_lint_clean_on_every_theme(self):
        for name in theme.THEMES:
            with self.subTest(theme=name):
                data = pptx_ops.create(DECK.format(theme=name))
                self.assertEqual(lint.lint_deck(data), [], name)

    def test_tones_follow_the_theme(self):
        dark, light = theme.THEMES["midnight"], theme.THEMES["corporate"]
        self.assertEqual(design._tone(dark, "blue")[0], dark["accent"])
        self.assertEqual(design._tone(dark, "green")[1], dark["surface"])        # no pastel card on a dark deck
        self.assertEqual(design._tone(light, "green"), design._TONES_LIGHT["green"])
        self.assertEqual(design._tone(light, "nonsense"), design._tone(light, "blue"))


NEW_LAYOUTS = (
    "# Onboarding Programme\nsubtitle: review\ntheme: {theme}\n---\n"
    "## How a merchant goes live\nlayout: process\n- Apply | Merchant submits the form and trade licence\n"
    "- Verify | Risk team checks documents within two days\n- Integrate | Sandbox keys issued and test payments run\n"
    "- Go live | Production keys and first settlement\n---\n"
    "## Where to focus\nlayout: matrix\n- Quick wins | High value, low effort\n- Big bets | High value, high effort\n"
    "- Fill-ins | Low value, low effort\n- Avoid | Low value, high effort\n---\n"
    "## Conversion funnel\nlayout: funnel\n- Applied | 1,200 merchants\n- Verified | 860 merchants (72%)\n"
    "- Integrated | 540 merchants (45%)\n- Live | 410 merchants (34%)\n---\n"
    "## Share by channel\nchart: doughnut | highlight=MFS\n- Cards: 38\n- MFS: 45\n- Net banking: 17\n---\n"
    "## Monthly volume\nchart: bar | highlight=Jun | xlabel=Month | ylabel=Transactions\n- Apr: 310\n- May: 342\n- Jun: 398\n---\n"
    "## Trend\nchart: area\n- Q1: 10\n- Q2: 14\n- Q3: 19\n---\n"
    "## What we shipped\nlayout: cards\n- icon=speed | Faster checkout | Payment time fell to 2.6 s\n"
    "- icon=security | Fraud screening | New rules blocked 1,240 attempts\n- icon=people | Portal | 310 merchants self-serve\n---\n"
    "## In numbers\nlayout: stats\n- icon=growth | 4.2M | transactions\n- icon=target | 97.4% | success rate\n---\n## Thank you\n")


class NewLayoutsTests(unittest.TestCase):
    def test_lint_clean_on_every_theme(self):
        for name in theme.THEMES:
            with self.subTest(theme=name):
                data = pptx_ops.create(NEW_LAYOUTS.format(theme=name))
                self.assertEqual(lint.lint_deck(data), [], name)
                self.assertEqual(notes.summary(data), "", name)

    def test_kinds_and_shapes(self):
        from core.doc_ops.pptx_ops import outline
        from pptx import Presentation
        import io
        deck = outline.parse_outline(NEW_LAYOUTS.format(theme="corporate"))
        self.assertEqual([s["kind"] for s in deck["slides"]][1:9],
                         ["process", "matrix", "funnel", "chart", "chart", "chart", "cards", "stats"])
        prs = Presentation(io.BytesIO(pptx_ops.create(NEW_LAYOUTS.format(theme="corporate"))))
        names = [sh.name for sl in prs.slides for sh in sl.shapes]
        for want in ("Step 1", "Quadrant 4", "Stage 4", "Icon speed", "Icon risk", "Icon growth"):
            self.assertIn(want, names)

    def test_wrong_item_count_is_reported(self):
        said = notes.summary(pptx_ops.create("# D\nsubtitle: s\n---\n## M\nlayout: matrix\n- A | x\n- B | y\n- C | z\n---\n## End\n"))
        self.assertIn("exactly 4 items", said)

    def test_unknown_icon_is_reported_and_aliases_resolve(self):
        from core.doc_ops.pptx_ops import icons
        self.assertEqual(icons.resolve("rocket")[0], "growth")
        self.assertEqual(icons.take("icon=growth | Revenue | up 18%"), ("Revenue | up 18%", "growth"))
        self.assertEqual(icons.take("Revenue | up 18%"), ("Revenue | up 18%", ""))
        said = notes.summary(pptx_ops.create("# D\nsubtitle: s\n---\n## C\nlayout: cards\n- icon=unicorn | Alpha plan | the first option in detail\n"
                                             "- Beta plan | the second option in detail\n- Gamma plan | the third option in detail\n---\n## Next\n- one\n- two\n---\n## Thank you\n"))
        self.assertIn("icon 'unicorn' is not available", said)

    def test_overfull_cards_are_split_and_reported(self):
        long = "A long explanation of this topic that keeps going with details, figures and names. " * 4
        md = "# D\nsubtitle: s\n---\n## Six\nlayout: cards\n" + "".join(f"- Topic {i}\n\t- {long}\n" for i in range(6)) + "---\n## End\n"
        data = pptx_ops.create(md)
        self.assertIn("split over two", notes.summary(data))
        self.assertEqual(lint.lint_deck(data), [])

    def test_bengali_text_names_a_complex_script_font(self):
        from pptx import Presentation
        import io
        prs = Presentation(io.BytesIO(pptx_ops.create("# D\nsubtitle: s\n---\n## বাংলা\n- ঢাকা বাংলাদেশের রাজধানী\n- দ্বিতীয় লাইন\n---\n## End\n")))
        xml = "".join(sh._element.xml for sl in prs.slides for sh in sl.shapes)
        self.assertIn('<a:cs typeface="Nirmala UI"/>', xml)


class PagePicturesTests(unittest.TestCase):
    """/agent/doc-pages and /agent/doc-page: the rendered file is kept per file version; no renderer means 0 pages."""
    def test_rendered_once_and_counted(self):
        import io
        from unittest import mock
        from reportlab.pdfgen import canvas
        from routes.agent import preview
        buf = io.BytesIO()
        c = canvas.Canvas(buf)
        for _ in range(3):
            c.drawString(72, 720, "page")
            c.showPage()
        c.save()
        preview._page_pdfs.clear()
        with mock.patch("core.doc_tools.render.to_pdf", return_value=buf.getvalue()) as conv:
            pdf, key = preview._rendered_pdf(b"deck bytes")
            self.assertIsNone(pdf)
            self.assertEqual(preview._render_now(b"deck bytes", ".pptx", key)[1], 3)
            self.assertEqual(preview._rendered_pdf(b"deck bytes")[1], 3)          # second look: from the cache
            self.assertEqual(conv.call_count, 1)
        with mock.patch("core.doc_tools.render.to_pdf", return_value=None):
            self.assertEqual(preview._render_now(b"other", ".pptx", "k"), (None, 0))


class BuildNotesTests(unittest.TestCase):
    def test_changes_are_reported(self):
        md = ("# Deck\nsubtitle: s\n---\n## Growth\nchart: bar\n- strong year\n- good team\n---\n## Wide\n"
              "| a | b | c | d | e | f | g | h |\n|---|---|---|---|---|---|---|---|\n| 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 |\n---\n## Thank you\n")
        said = notes.summary(pptx_ops.create(md))
        self.assertIn("no chart was drawn", said)
        self.assertIn("first 6 of 8 columns", said)
        self.assertIn("doc_create", said)

    def test_a_faithful_build_says_nothing(self):
        self.assertEqual(notes.summary(pptx_ops.create("# Deck\nsubtitle: s\n---\n## One\n- a\n- b\n- c\n---\n## Thank you\n")), "")

    def test_a_long_table_keeps_every_row(self):
        rows = "\n".join(f"| item {i} | {i * 10} |" for i in range(60))
        from pptx import Presentation
        import io
        prs = Presentation(io.BytesIO(pptx_ops.create(f"# Deck\nsubtitle: s\n---\n## Big\n| Name | Value |\n|---|---|\n{rows}\n---\n## End\n")))
        cells = [c.text for sl in prs.slides for sh in sl.shapes if getattr(sh, "has_table", False) and sh.has_table
                 for r in sh.table.rows for c in r.cells]
        for i in (0, 30, 59):
            self.assertIn(f"item {i}", cells)


KPI_ITEMS = ["- 97.4% | ^ 1.2 pts | Success rate | tone=green", "- 4.2M | Transactions | tone=navy",
             "- 310 ms | Latency | tone=blue #2", "- #2 | Rank two | tone=amber"]
ROAD_ITEMS = ["- 1 | Pilot | Jan 2026 | Ten merchants | status=done", "- 2 | Launch | Apr 2026 | All merchants | status=now",
              "- 3 | Scale | Sep 2026 | Regional | status=planned"]


def _deck(body: str, lay: str = "", theme_name: str = "") -> str:
    head = "# Deck\nsubtitle: s\n" + (f"theme: {theme_name}\n" if theme_name else "") + "---\n## Slide\n"
    return head + (f"layout: {lay}\n" if lay else "") + body + "\n---\n## Other\n- one\n- two\n---\n## Thank you\n"


def _slide_texts(data: bytes, n: int = 1) -> list:
    import io
    from pptx import Presentation
    out = []
    for sh in Presentation(io.BytesIO(data)).slides[n].shapes:
        if sh.has_text_frame:
            out += [p.text for p in sh.text_frame.paragraphs if p.text.strip()]
        if getattr(sh, "has_table", False) and sh.has_table:
            out += [c.text for r in sh.table.rows for c in r.cells]
    return out


def _kind(md: str, n: int = 1) -> str:
    from core.doc_ops.pptx_ops import outline
    return outline.parse_outline(md)["slides"][n]["kind"]


class BuildFieldsNeverPrintTests(unittest.TestCase):
    """`tone=`, `status=`, `icon=` and raw `|` are build syntax: no layout may print them."""

    @classmethod
    def cases(cls):
        out = {}
        for lay in ("", "kpi-grid", "stats", "cards", "bullets", "chart-kpis", "two-column", "agenda", "timeline", "comparison",
                    "leaderboard", "process", "matrix", "funnel", "roadmap", "dimension-table", "textside"):
            body = "\n".join(ROAD_ITEMS if lay == "roadmap" else KPI_ITEMS)
            extra = "chart: bar\n| A | B |\n|---|---|\n| x | 1 |\n| y | 2 |\n" if lay in ("chart-kpis", "textside", "dimension-table") else ""
            out[lay or "(none)"] = _deck(extra + body, lay)
        for lay in ("", "timeline", "cards", "bullets", "process"):
            out["road/" + (lay or "(none)")] = _deck("\n".join(ROAD_ITEMS), lay)
        return out

    def test_no_slide_text_contains_a_field_or_pipe(self):
        import re
        bad = re.compile(r"tone\s*=|status\s*=|icon\s*=|\|")
        for name, md in self.cases().items():
            with self.subTest(case=name):
                leaks = [t for t in _slide_texts(pptx_ops.create(md)) if bad.search(t)]
                self.assertEqual(leaks, [], name)

    def test_layout_is_chosen_from_the_fields(self):
        self.assertEqual(_kind(_deck("\n".join(KPI_ITEMS))), "kpi-grid")
        self.assertEqual(_kind(_deck("\n".join(ROAD_ITEMS))), "roadmap")
        self.assertEqual(_kind(_deck("\n".join(ROAD_ITEMS), "cards")), "roadmap")
        self.assertEqual(_kind(_deck("\n".join(KPI_ITEMS), "stats")), "kpi-grid")
        # a plain two-part stats slide is still stats, an explicit other layout is left alone
        self.assertEqual(_kind(_deck("- 4.2M | Transactions\n- 97% | Success\n- 310 ms | Latency")), "stats")
        self.assertEqual(_kind(_deck("\n".join(KPI_ITEMS), "process")), "process")
        self.assertNotEqual(_kind(_deck("- 2019 | Founded | in Dhaka\n- 2021 | Series A | raised\n- 2024 | Listed | on the exchange")), "kpi-grid")

    def test_split_fields_removes_whole_segments(self):
        from core.doc_ops.pptx_ops import outline
        self.assertEqual(outline.split_fields("310 ms | Latency | tone=blue #2"), ("310 ms | Latency", {"tone": "blue #2"}))
        self.assertEqual(outline.split_fields("icon=growth | Revenue | status = done"),
                         ("Revenue", {"icon": "growth", "status": "done"}))
        self.assertEqual(outline.split_fields("Plain | text"), ("Plain | text", {}))

    def test_plain_layouts_read_naturally(self):
        texts = _slide_texts(pptx_ops.create(_deck("\n".join(KPI_ITEMS), "bullets").replace("layout: bullets\n", "layout: bullets\nimage: x\n")))
        self.assertIn("97.4%: ↑ 1.2 pts · Success rate", " ".join(texts))

    def test_stats_keep_value_caption_and_icon(self):
        import io
        from pptx import Presentation
        data = pptx_ops.create(_deck("- icon=growth | 4.2M | transactions | tone=navy\n- icon=target | 97.4% | success rate", "stats"))
        texts = _slide_texts(data)
        for want in ("4.2M", "transactions", "97.4%", "success rate"):
            self.assertIn(want, texts)
        names = [sh.name for sh in Presentation(io.BytesIO(data)).slides[1].shapes]
        self.assertIn("Icon growth", names)
        self.assertIn("Icon target", names)

    def test_stats_put_the_change_after_the_caption(self):
        from core.doc_ops.pptx_ops import outline
        s = {"kind": "stats", "bullets": ["97.4% | ^ 1.2 pts | Success rate | tone=green"]}
        outline._normalise(s)
        self.assertEqual(s["bullets"], ["97.4% | Success rate · ↑ 1.2 pts"])

    def test_kpi_tiles_show_value_delta_caption(self):
        import io
        from pptx import Presentation
        prs = Presentation(io.BytesIO(pptx_ops.create(_deck("\n".join(KPI_ITEMS)))))
        tiles = [[p.text for p in sh.text_frame.paragraphs] for sh in prs.slides[1].shapes if sh.name.startswith("KPI Card")]
        self.assertEqual(tiles, [["97.4%", "↑ 1.2 pts", "Success rate"], ["4.2M", "Transactions"],
                                 ["310 ms", "Latency"], ["#2", "Rank two"]])

    def test_kpi_part_rules(self):
        from core.doc_ops.pptx_ops import outline
        kp = outline.kpi_parts
        self.assertEqual(kp("4.2M | tone=navy"), ("4.2M", "", "", "navy"))
        self.assertEqual(kp("4.2M | v 3% | Dispute rate"), ("4.2M", "↓ 3%", "Dispute rate", ""))
        self.assertEqual(kp("4.2M | Median response time | per request"), ("4.2M", "", "Median response time · per request", ""))
        self.assertEqual(kp("4.2M | ^ 3%"), ("4.2M", "↑ 3%", "", ""))
        self.assertEqual(kp("12 | up 4 pts | Merchants | Dhaka"), ("12", "up 4 pts", "Merchants · Dhaka", ""))

    def test_chart_kpi_cards_have_no_fields(self):
        md = ("# D\nsubtitle: s\n---\n## C\nlayout: chart-kpis\nchart: bar\n| A | Q1 | Q2 |\n|---|---|---|\n| x | 1 | 2 |\n| y | 3 | 4 |\n"
              "- +25% | Card growth | tone=green\n- 97% | ^ 2 pts | Success | tone=blue #3\n---\n## End\n")
        texts = _slide_texts(pptx_ops.create(md))
        self.assertIn("Success · ↑ 2 pts", texts)
        self.assertFalse([t for t in texts if "tone" in t or "|" in t])

    def test_more_than_four_kpis_are_split_and_reported(self):
        items = "\n".join(f"- {i}0% | Metric {i} | tone=blue" for i in range(1, 7))
        data = pptx_ops.create(_deck(items))
        from pptx import Presentation
        import io
        prs = Presentation(io.BytesIO(data))
        self.assertEqual(len(prs.slides), 5)
        self.assertIn("split over two", notes.summary(data))
        self.assertEqual(lint.lint_deck(data), [])

    def test_lint_clean_on_every_theme(self):
        for name in theme.THEMES:
            for case, md in self.cases().items():
                if case == "leaderboard":          # its own fixed-size layout, not touched here (these items are not profiles)
                    continue
                with self.subTest(theme=name, case=case):
                    self.assertEqual(lint.lint_deck(pptx_ops.create(md.replace("subtitle: s", f"theme: {name}\nsubtitle: s", 1))), [])


AUTO_SAMPLE = (
    "# Auto\nsubtitle: s\ntheme: {theme}\n---\n"
    "## Scorecard\n" + "\n".join(KPI_ITEMS) + "\n---\n"
    "## Plan\n" + "\n".join(ROAD_ITEMS) + "\n---\n"
    "## Volume by channel\n- Cards: 380\n- MFS: 450\n- Net banking: 170\n- Wallets: 120\n---\n"
    "## Share of volume\n- Cards: 38%\n- MFS: 45%\n- Net banking: 17%\n---\n"
    "## Quarterly revenue\n- Q1: 10\n- Q2: 14\n- Q3: 19\n- Q4: 25\n---\n"
    "## Monthly volume\n| Month | Online | Offline |\n|---|---|---|\n| Jan | 310 | 240 |\n| Feb | 342 | 260 |\n| Mar | 398 | 300 |\n"
    "- Volume grew every month\n---\n"
    "## Notes\n- Merchants onboarded | tone=green\n- Support load stable | tone=amber\n- Fraud screening live\n---\n## Thank you\n")


class AutoChartTests(unittest.TestCase):
    def slide(self, body, lay=""):
        from core.doc_ops.pptx_ops import outline
        return outline.parse_outline(_deck(body, lay))["slides"][1]

    def test_label_number_bullets_become_a_bar_chart(self):
        s = self.slide("- Cards: 380\n- MFS: 450\n- Net banking: 170")
        self.assertEqual((s["kind"], s["chart_data"]["kind"]), ("chart", "bar"))
        self.assertEqual(self.slide("- Cards: 380\n- MFS: 450\n- Net banking: 170", "bullets")["kind"], "chart")

    def test_percentages_summing_to_100_are_a_doughnut(self):
        s = self.slide("- Cards: 38%\n- MFS: 45%\n- Net banking: 17%")
        self.assertEqual((s["kind"], s["chart_data"]["kind"], s["chart_data"]["unit"]), ("chart", "doughnut", "%"))
        self.assertEqual(self.slide("- Cards: 30%\n- MFS: 45%\n- Net banking: 10%")["chart_data"]["kind"], "bar")

    def test_time_series_are_lines(self):
        for body in ("- Q1: 10\n- Q2: 14\n- Q3: 19\n- Q4: 25", "- Q1 2025: 10\n- Q2 2025: 14\n- Q3 2025: 19",
                     "- Jan: 10\n- Feb: 14\n- Mar: 19", "- 2021: 10\n- 2022: 14\n- 2023: 19"):
            with self.subTest(body=body):
                self.assertEqual(self.slide(body)["chart_data"]["kind"], "line")
        self.assertEqual(self.slide("- Mar: 10\n- Feb: 14\n- Jan: 19")["chart_data"]["kind"], "bar")      # not in order

    def test_numeric_table_gets_a_chart(self):
        table = "| Month | Online | Offline |\n|---|---|---|\n| Jan | 310 | 240 |\n| Feb | 342 | 260 |\n| Mar | 398 | 300 |"
        s = self.slide(table)
        self.assertEqual((s["kind"], s["chart_data"]["kind"], len(s["chart_data"]["series"])), ("chart", "line", 2))
        s = self.slide(table + "\n- Volume grew every month")
        self.assertEqual(s["kind"], "textside")
        self.assertTrue(s["chart_data"])

    def test_charts_are_skipped_when_numbers_are_not_comparable(self):
        for body in ("- Success: 97%\n- Latency: 310 ms\n- Volume: 12",                 # mixed units
                     "- Founded: 1998\n- Listed: 2010\n- Pivot: 2015",                  # years as values
                     "- Cards: 380\n- MFS: 450",                                         # two points
                     "- Cards: 380\n- MFS: 450\n- Notes about the rest",                # one bullet is text
                     "- Cards: 380\n\t- detail\n- MFS: 450\n- Wallets: 120",             # sub-bullets
                     "| Area | Score | Status |\n|---|---|---|\n| Sec | 4.5 | Ready |\n| Sup | 3.1 | Bad |\n| Ops | 2 | Ok |",
                     "| Month | Temp | Rain |\n|---|---|---|\n| Jan | 18 | 7 |\n| Apr | 28 | 128 |\n| Jul | 29 | 391 |"):
            with self.subTest(body=body):
                self.assertNotEqual(self.slide(body)["kind"], "chart")
                self.assertNotIn("chart_data", self.slide(body))

    def test_explicit_layouts_are_untouched(self):
        self.assertEqual(self.slide("- Cards: 380\n- MFS: 450\n- Wallets: 120", "cards")["kind"], "cards")
        self.assertEqual(self.slide("- Cards: 380\n- MFS: 450\n- Wallets: 120", "stats")["kind"], "bullets")

    def test_switch_off(self):
        from unittest import mock
        from core.doc_ops.pptx_ops import outline
        with mock.patch.object(outline, "AUTO_CHART", False):
            self.assertNotEqual(self.slide("- Cards: 380\n- MFS: 450\n- Wallets: 120")["kind"], "chart")

    def test_automatic_upgrade_is_silent_and_lint_clean_on_every_theme(self):
        for name in theme.THEMES:
            with self.subTest(theme=name):
                data = pptx_ops.create(AUTO_SAMPLE.format(theme=name))
                self.assertEqual(lint.lint_deck(data), [], name)
                self.assertEqual(notes.summary(data), "", name)
                self.assertEqual([t for t in _slide_texts(data, 1) if "tone" in t], [])


if __name__ == "__main__":
    unittest.main()
