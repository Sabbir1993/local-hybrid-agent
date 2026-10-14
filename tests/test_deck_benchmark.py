"""Quality benchmark: a fixed set of decks (varied content, long text, tables, charts, Bengali) must come out lint-clean.

This is the measurable part of "good layout": every deck below is built with every theme and read back through the layout
check (core/doc_ops/pptx_ops/lint.py): no overflow, no shape off the slide, no overlapping text, no text under 11 pt, no
low contrast. A regression in the fitting, splitting or colour code shows up here as a named finding.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.doc_ops import pptx_ops
from core.doc_ops.pptx_ops import lint

THEMES = ("midnight", "corporate", "forest", "sunset", "minimal", "royal")
LONG = "Founded in the seventeenth century by Mughal governors and expanded under later rulers of the region"
BN = "ঢাকা বাংলাদেশের রাজধানী এবং বৃহত্তম শহর, যেখানে এক কোটিরও বেশি মানুষ বাস করে"


def bullets(n, text=LONG):
    return "\n".join(f"- {text} {i}" for i in range(n))


DECKS = {
    "short": "# Short\nsubtitle: two slides\n---\n## Only\n- one\n- two\n- three\n---\n## Thank you\n",
    "long bullets": f"# Long\nsubtitle: s\n---\n## Landmarks\n{bullets(14)}\n---\n## More\n{bullets(9)}\n---\n## Thank you\n",
    "long titles": ("# A very long deck title that goes on and on about the risk and resilience of the enterprise in 2026\n"
                    "subtitle: an equally long subtitle that explains the whole story of the briefing in one line\n---\n"
                    "## A slide title so long that it needs to wrap onto a second line and maybe a third line too\n"
                    "- alpha\n- beta\n- gamma\n---\n## Thank you\n"),
    "tables": ("# Tables\nsubtitle: s\n---\n## Small\n| A | B |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n---\n## Wide\n"
               "| City | People | Area | Density | Founded | Region |\n|---|---|---|---|---|---|\n"
               + "".join(f"| City {i} with a long name | {i}M | {i * 10} | {i * 100} | 16{i:02d} | Division {i} |\n" for i in range(1, 8))
               + "---\n## Tall\n| Id | Description |\n|---|---|\n"
               + "".join(f"| {i} | a longer description of item number {i} that wraps in the cell |\n" for i in range(1, 22))
               + "---\n## Thank you\n"),
    "charts": ("# Charts\nsubtitle: s\n---\n## Line\nchart: line | unit=%\n- 2024: 40%\n- 2025: 60%\n- 2026: 89%\n---\n## Bars\n"
               "chart: bar\n| Year | Sales | Cost |\n|---|---|---|\n| 2024 | 10 | 7 |\n| 2025 | 14 | 9 |\n| 2026 | 20 | 11 |\n---\n"
               "## Pie\nchart: pie\n- A: 50\n- B: 30\n- C: 20\n---\n## Horizontal\nchart: hbar\n- Alpha: 12\n- Beta: 30\n- Gamma: 21\n---\n"
               "## Thank you\n"),
    "layout mix": (f"# Mix\nsubtitle: s\n---\n## Agenda\nlayout: agenda\n- One\n- Two\n- Three\n- Four\n- Five\n- Six\n---\n"
                   "## Part one\nlayout: section\n---\n## Compare\nlayout: comparison\n- Legacy:\n  - Slow to change\n  - Costly\n"
                   "- Modern:\n  - Fast to change\n  - Cheap\n---\n## Services\n- Speed: very fast\n- Cost: quite low\n- Safety: well tested\n---\n"
                   "## Numbers\n- 99.9% uptime\n- 5x faster\n- 40% cheaper\n---\n## Steps\nlayout: timeline\n- 2018: Founded\n"
                   "- 2020: Expanded\n- 2022: Listed\n- 2024: Global\n---\n## Quote\nlayout: quote\n- \"Simplicity is the ultimate sophistication\"\n"
                   "- Leonardo\n---\n## Two columns\nlayout: two-column\n- A\n- B\n- C\n- D\n- E\n- F\n- G\n- H\n---\n## Thank you\n"),
    "bengali": (f"# ঢাকা\nsubtitle: {BN}\n---\n## পরিচিতি\n- {BN}\n- {BN}\n- {BN}\n---\n## সংখ্যা\nchart: bar\n- ২০২৩: 10\n- ২০২৪: 20\n---\n"
                f"## ধন্যবাদ\n"),
    "numbers and cards": ("# Cards\nsubtitle: s\n---\n## Pillars\n- Reliability: systems stay up even when parts fail badly\n"
                          "- Security: every request is verified and logged\n- Speed: pages open in under a second\n"
                          "- Cost: pay only for what you use each month\n---\n## Results\n- $10.8T global impact\n- 29 minutes average breach time\n"
                          "- 12,000 attempts blocked\n---\n## Thank you\n"),
    "everything dense": f"# Dense\nsubtitle: s\n---\n## One\n{bullets(5)}\n---\n## Two\n{bullets(6)}\n---\n## Three\n{bullets(7)}\n---\n## Thank you\n",
}


class Benchmark(unittest.TestCase):
    def test_every_deck_in_every_theme_is_lint_clean(self):
        bad = []
        total = 0
        for name, md in DECKS.items():
            for theme in THEMES:
                data = pptx_ops.create(md.replace("subtitle:", f"theme: {theme}\nsubtitle:", 1))
                findings = lint.lint_deck(data)
                total += 1
                bad += [f"{name}/{theme}: {f['message']}" for f in findings]
        self.assertEqual(bad, [], f"{len(bad)} findings over {total} decks")


if __name__ == "__main__":
    unittest.main()
