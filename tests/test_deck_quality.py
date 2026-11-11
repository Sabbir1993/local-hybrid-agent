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
