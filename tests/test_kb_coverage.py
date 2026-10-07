"""tests/test_kb_coverage.py - KB-first, web-for-the-gap routing (core/kb_coverage.py) and the agent's
on-demand web tools (core/tool_surface.py). Run: python -m unittest tests.test_kb_coverage -v"""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import kb_coverage as kc, tool_surface
from core.small_model import APP_CONFIG


def hit(cos, title="Doc", text="text"):
    return {"cos": cos, "title": title, "text": text}


class _Cfg(unittest.TestCase):
    def setUp(self):
        self._saved = {k: APP_CONFIG.get(k) for k in ("knowledge", "chat")}
        APP_CONFIG["knowledge"] = {"full_cos": 0.70, "auto_inject_cos": 0.55}
        APP_CONFIG.pop("chat", None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                APP_CONFIG.pop(k, None)
            else:
                APP_CONFIG[k] = v


class ClassifyTests(_Cfg):
    def test_extremes_are_decided_without_a_model(self):
        self.assertEqual(kc.classify([]), "none")
        self.assertEqual(kc.classify([hit(0.82), hit(0.71), hit(0.4)]), "full")
        self.assertEqual(kc.classify([hit(0.50)]), "partial")           # company keyword, weak match
        self.assertEqual(kc.classify([hit(0.62), hit(0.58)]), "ambiguous")
        self.assertEqual(kc.classify([hit(0.9)]), "ambiguous")         # a single strong hit is not 'full'

    def test_ambiguous_without_a_judge_falls_back_to_partial(self):
        with mock.patch.object(kc, "judge", mock.AsyncMock(return_value=None)):
            self.assertEqual(asyncio.run(kc.resolve("q", [hit(0.62)])), "partial")

    def test_judge_decides_only_the_ambiguous_band(self):
        j = mock.AsyncMock(return_value="full")
        with mock.patch.object(kc, "judge", j):
            self.assertEqual(asyncio.run(kc.resolve("q", [hit(0.62)])), "full")
            self.assertEqual(asyncio.run(kc.resolve("q", [hit(0.9), hit(0.8)])), "full")
            self.assertEqual(asyncio.run(kc.resolve("q", [])), "none")
        self.assertEqual(j.await_count, 1)                              # the extremes never called it

    def test_judge_reads_the_small_model_answer_and_hides_text_when_blocked(self):
        seen = {}

        class Client:
            async def post(self, url, json, **kw):
                seen["body"] = json
                return SimpleNamespace(json=lambda: {"choices": [{"message": {"content": "<think>x</think>Partial."}}]})

        class Target:
            lane = "executor"
            def is_up(self): return True
            async def client(self): return Client()

        with mock.patch("core.lanes.targets", return_value=[Target()]):
            out = asyncio.run(kc.judge("what is X?", [hit(0.6, "HR", "SECRET-SALARY-TEXT")], blocked=True))
        self.assertEqual(out, "partial")
        self.assertNotIn("SECRET-SALARY-TEXT", str(seen["body"]))
        self.assertIn("HR", str(seen["body"]))

    def test_judge_unavailable_returns_none(self):
        with mock.patch("core.lanes.targets", return_value=[]):
            self.assertIsNone(asyncio.run(kc.judge("q", [hit(0.6)])))


class BudgetTests(_Cfg):
    def test_tier_follows_effort_and_deep(self):
        self.assertEqual(kc.tier(None, False), "medium")
        self.assertEqual(kc.tier("none", False), "low")
        self.assertEqual(kc.tier("low", False), "low")
        self.assertEqual(kc.tier("high", False), "high")
        self.assertEqual(kc.tier("extra", False), "deep")
        self.assertEqual(kc.tier("low", True), "deep")

    def test_default_table_grows_with_thinking(self):
        for row in ("partial", "open"):
            vals = [kc.web_budget("partial" if row == "partial" else "none", t, explicit=(row == "open"))
                    for t in kc.TIERS]
            self.assertEqual(vals, sorted(vals))
            self.assertGreater(vals[-1], vals[0])
        self.assertEqual(kc.web_budget("full", "medium"), 0)
        self.assertEqual(kc.web_budget("full", "deep"), 3)
        self.assertEqual(kc.web_budget("none", "deep", explicit=True), 20)

    def test_config_overrides_and_bad_values_fall_back(self):
        APP_CONFIG["chat"] = {"web_budget": {"partial": {"medium": 5, "high": "oops"}}}
        self.assertEqual(kc.web_budget("partial", "medium"), 5)
        self.assertEqual(kc.web_budget("partial", "high"), kc.DEFAULT_BUDGET["partial"]["high"])
        APP_CONFIG["chat"] = {"web_budget": {"partial": {"medium": 9999}}}
        self.assertEqual(kc.web_budget("partial", "medium"), 100)       # clamped

    def test_rounds_leave_room_for_the_budget(self):
        self.assertEqual(kc.rounds_for(20, 20), 24)
        self.assertEqual(kc.rounds_for(3, 12), 12)
        self.assertEqual(kc.rounds_for(0, 12), 12)


class DecideWebTests(_Cfg):
    def d(self, cov, tier="medium", web_on=True, explicit=False):
        return kc.decide_web(cov, tier, web_on, explicit)

    def test_full_coverage_costs_nothing_unless_asked_or_deep(self):
        self.assertFalse(self.d("full")["web"])
        self.assertTrue(self.d("full", explicit=True)["web"])
        deep = self.d("full", "deep")
        self.assertTrue(deep["web"] and deep["lean"] and deep["budget"] == 3)

    def test_partial_gets_lean_web_with_a_small_budget(self):
        p = self.d("partial")
        self.assertEqual((p["web"], p["lean"], p["budget"]), (True, True, 3))
        self.assertEqual(self.d("partial", "deep")["budget"], 12)
        self.assertEqual(self.d("partial", "low")["budget"], 2)

    def test_none_needs_explicit_intent_or_deep(self):
        self.assertFalse(self.d("none")["web"])
        e = self.d("none", explicit=True)
        self.assertEqual((e["web"], e["lean"], e["budget"]), (True, False, 8))
        self.assertTrue(self.d("none", "deep")["web"])

    def test_web_switch_off_always_wins(self):
        for cov in ("full", "partial", "none"):
            self.assertFalse(self.d(cov, "deep", web_on=False, explicit=True)["web"])


class HygieneTests(unittest.TestCase):
    def test_query_key_ignores_order_case_and_stopwords(self):
        self.assertEqual(kc.query_key("Brac Bank branch list"), kc.query_key("list of BRAC bank branches"))
        self.assertNotEqual(kc.query_key("brac bank"), kc.query_key("city bank"))

    def test_late_results_are_trimmed_early_ones_are_not(self):
        big = "x" * 9000
        self.assertEqual(kc.cap_web_result(big, 2), big)
        self.assertTrue(kc.cap_web_result(big, 6).endswith("within context]"))
        self.assertLess(len(kc.cap_web_result(big, 6)), 6100)
        self.assertEqual(kc.cap_web_result("short", 9), "short")


class AgentWebSurfaceTests(unittest.TestCase):
    TOOLS = [{"function": {"name": n}} for n in ("read_file", "web_search", "web_fetch", "web_search_images", "browser_click")]

    def names(self, q, cfg):
        return [t["function"]["name"] for t in tool_surface.filter_tools(self.TOOLS, q, cfg)]

    def test_flag_off_keeps_web_as_a_core_tool(self):
        self.assertIn("web_search", self.names("fix the failing test", None))
        self.assertNotIn("web", tool_surface.hidden_families("fix the failing test", None))

    def test_on_demand_hides_web_until_wanted(self):
        cfg = {"web_on_demand": True}
        self.assertNotIn("web_search", self.names("fix the failing test", cfg))
        self.assertIn("web", tool_surface.hidden_families("fix the failing test", cfg))
        self.assertIn("web_search", self.names("look up the latest release notes", cfg))
        self.assertIn("web_fetch", self.names("summarise https://example.com/a", cfg))
        self.assertIn("web_search", self.names("fix the failing test", {**cfg, "web_extra": True}))
        self.assertNotIn("web", tool_surface.hidden_families("fix the failing test", {**cfg, "web_extra": True}))

    def test_a_conversation_that_used_web_keeps_it(self):
        msgs = [{"role": "user", "content": "ok go on"},
                {"role": "assistant", "tool_calls": [{"function": {"name": "web_search"}}]}]
        self.assertIn("web_search", self.names(tool_surface.surface_query(msgs), {"web_on_demand": True}))


if __name__ == "__main__":
    unittest.main()
