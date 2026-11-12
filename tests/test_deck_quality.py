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


if __name__ == "__main__":
    unittest.main()
