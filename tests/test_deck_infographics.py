import unittest
from core.doc_ops.pptx_ops import outline, design


class TestDeckInfographics(unittest.TestCase):
    def test_parse_and_render_all_5_layouts(self):
        md = """theme: corporate
# AI Transformation Executive Report
subtitle: Comprehensive Analysis and Progression
---
## Maturity Level Distribution
eyebrow: SECTION 01 · LEVEL PROGRESSION
subtitle: March vs May 2026 — matched cohort n = 93
layout: chart-kpis
chart: bar | unit=
| Level | March 2026 | May 2026 |
| L1 | 0 | 0 |
| L2 | 3 | 1 |
| L3 | 72 | 47 |
| L4 | 22 | 43 |
| L5 | 3 | 6 |
- +38 | Moved up >=1 level | tone=green
- +4 | Jumped 2 levels | tone=blue
- 45 | Held same level | tone=gray
- 10 | Regressed 1 level | tone=red
source: SSLWIRELESS · AI-First Programme · May 2026
---
## AI Champions — Top Performers
eyebrow: SECTION 03 · AI CHAMPIONS
subtitle: 6 Level 5 Natives · 4 L4 Proficients · 10 champions total
layout: leaderboard
- GS | 3.94 | L5 Native | Gazi Md. Shahnewaz | Top scorer · consistent programme leader
- UK | 3.75 | L5 Native | Ujjwal Kumar Gupta | +1.72 highest jump · L3 -> L5
- SH | 3.70 | L5 Native | Sabbir Hosain Chowdury | Stable top · L5 in both surveys
- SC | 3.58 | L5 Native | Suman Chandra Das | New L5 · architecture-level leverage
- AC | 3.58 | L5 Native | Md. Abu Sayed Chowdhury | Promoted L4 -> L5 (+0.43)
- DK | 3.51 | L5 Native | Dipto Kumar Kundu | +1.30 · jumped L3 -> L5
- SL | 3.46 | L4 Proficient | Shawon Lodh | +1.43 biggest L3->L4 score gain
- AI | 3.43 | L4 Proficient | Ariful Islam | Consistent performer +0.32
- SD | 3.42 | L4 Proficient | Sumon Deb | +0.96 from L3 base
- SR | 3.40 | L4 Proficient | S. M. Shahidur Harun Rumy | Stable high performer
---
## Programme at a Glance
eyebrow: EXECUTIVE SUMMARY
subtitle: 45-day upskilling cohort · March -> May 2026 · 97 responses
layout: kpi-grid
- 2.92 | ^ +0.27 from 2.65 | Avg Composite Score (May) | tone=blue
- 38 | ^ 41% of cohort | Engineers Levelled Up | tone=green
- 6 | ^ Doubled from 3 | Level 5 AI Natives | tone=navy
- 49 | L4+ engineers | Now at Proficient or Above | tone=amber
callout: The AI-First Programme enrolled 100 engineers in March 2026 for a structured 45-day upskilling cohort. Average composite rose from 2.65 to 2.92 (+10.2%), and the Level 4-5 population nearly doubled from 25 to 49. The Data team has turned net-negative (-0.01 delta) driven by three regressions — requiring urgent department-level intervention.
---
## Dimension-Level Performance
eyebrow: SECTION 04 · DIMENSION ANALYSIS
subtitle: All 5 dimensions improved — ARCI highest relative gain, GD lowest
layout: dimension-table
| DIM | Description | Mar | May | Delta | Status |
| AAI | AI Adoption & Integration (Q1-5) | 2.82 | 3.08 | +0.26 | Strong |
| AVI | AI Velocity & Impact (Q6-10) | 2.65 | 2.94 | +0.29 | Strong |
| ARCI | AI Risk & Compliance (Q13-16) | 2.41 | 2.72 | +0.31 | Lowest |
| GD | Growth & Development (Q17-19) | 2.75 | 2.87 | +0.12 | Moderate |
| ECW | Craft & Cost Wellbeing (Q20-21) | 2.72 | 2.92 | +0.20 | Good |
alert: ARCI (2.72) is lowest — critical for PCI-DSS. Mandatory AI governance refresher required before July sprint.
---
## AI Adoption Roadmap
eyebrow: SECTION 05 · ROADMAP
subtitle: 8-milestone journey from foundation to AI-native operations
layout: roadmap
- 1 | Foundation | Jan-Feb 2026 | Baseline survey · Tool provisioning · Governance setup | status=done
- 2 | Wave 1 Assess | Mar 2026 | 100 engineers · Avg 2.65 · L3=72, L4=22, L5=3 | status=done
- 3 | Upskill Sprint | Mar-Apr 2026 | Prompt labs · Agentic AI · Champion-led peer sessions | status=done
- 4 | Wave 2 Assess | May 2026 | 93 matched · Avg 2.92 · 38 up · 6 L5 Natives | status=now
- 5 | Intervention | Jun 2026 | 23 flagged engineers · Data team workshop | status=planned
- 6 | Agentic Track | Jul 2026 | L4-L5 advanced cohort · Target: 15 at L5 | status=planned
- 7 | Wave 3 + Board | Aug 2026 | Full re-assess · Instrumented Board report | status=planned
- 8 | AI-Native Ops | Q4 2026 | AI in hiring, perf reviews, dashboards | status=planned
---
## Next Steps
- Continue agentic track execution across engineering
- Address data team governance requirements
---
## Thank you
- Questions & Discussion
"""
        deck = outline.parse_outline(md)
        slides = deck["slides"]
        self.assertEqual(len(slides), 8)
        self.assertEqual(slides[0]["kind"], "cover")
        self.assertEqual(slides[1]["kind"], "chart-kpis")
        self.assertEqual(slides[2]["kind"], "leaderboard")
        self.assertEqual(slides[3]["kind"], "kpi-grid")
        self.assertEqual(slides[4]["kind"], "dimension-table")
        self.assertEqual(slides[5]["kind"], "roadmap")

        # Now build real presentation
        prs = design.build(deck)
        self.assertIsNotNone(prs)
        self.assertEqual(len(prs.slides), 8)

        # Verify shapes on chart-kpis slide
        s1 = prs.slides[1]
        shape_names = [shp.name for shp in s1.shapes]
        self.assertTrue(any("Chart" in n for n in shape_names))
        self.assertTrue(any("Delta Card" in n for n in shape_names))
        self.assertTrue(any("eyebrow" in n for n in shape_names))

        # Verify shapes on leaderboard slide
        s2 = prs.slides[2]
        shape_names2 = [shp.name for shp in s2.shapes]
        self.assertTrue(any("Profile Card" in n for n in shape_names2))
        self.assertTrue(any("deco-av" in n for n in shape_names2))

        # Verify shapes on kpi-grid slide
        s3 = prs.slides[3]
        shape_names3 = [shp.name for shp in s3.shapes]
        self.assertTrue(any("KPI Card" in n for n in shape_names3))
        self.assertTrue(any("Executive Callout" in n for n in shape_names3))

        # Verify shapes on dimension-table slide
        s4 = prs.slides[4]
        shape_names4 = [shp.name for shp in s4.shapes]
        self.assertTrue(any("Table" in n for n in shape_names4))
        self.assertTrue(any("Alert Banner" in n for n in shape_names4))

        # Verify shapes on roadmap slide
        s5 = prs.slides[5]
        shape_names5 = [shp.name for shp in s5.shapes]
        self.assertTrue(any("Node" in n for n in shape_names5))
        self.assertTrue(any("Station Card" in n for n in shape_names5))
        self.assertTrue(any("Legend Label" in n for n in shape_names5))


if __name__ == "__main__":
    unittest.main()
