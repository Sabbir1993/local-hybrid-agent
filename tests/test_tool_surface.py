"""Per-lane tool surface (core/tool_surface.py) and the small-model KV cap.

Measured motivation: the full registry is 48 tools / ~8,527 tokens of schema, sent
on every step of every run - about a quarter of a 32,768 window before the
conversation starts. Coding tasks never need browser automation, device
automation, office-document editing or image generation, yet paid for them.

These tests pin the on-demand behaviour, the escape hatches (the flag can be
turned off, MCP tools are never hidden, a custom-agent allowlist still wins by
being applied later) and the measured saving.

Run: python -m unittest tests.test_tool_surface -v
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import tool_surface as ts  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
AGENT_SRC = REPO / "routes" / "agent.py"


def _tool(name):
    return {"type": "function", "function": {"name": name, "description": "d" * 50,
                                             "parameters": {"type": "object", "properties": {}}}}


class TestFamilies(unittest.TestCase):

    def test_a_plain_coding_request_asks_for_nothing(self):
        for q in ("fix the failing test in core/db.py",
                  "refactor this function and update the plan",
                  "why does the import fail?"):
            with self.subTest(q=q):
                self.assertEqual(ts.needed_families(q), set())

    def test_browser_words(self):
        for q in ("log into my dashboard and take a screenshot",
                  "scrape the prices from that web page",
                  "use the browser to click through the wizard"):
            with self.subTest(q=q):
                self.assertIn("browser", ts.needed_families(q))

    def test_document_words(self):
        for q in ("summarize this excel spreadsheet", "edit the powerpoint deck",
                  "read the pdf and fix the numbers"):
            with self.subTest(q=q):
                self.assertIn("docs", ts.needed_families(q))

    def test_device_words(self):
        for q in ("run the tests on my android emulator", "take a screenshot of the app screen"):
            with self.subTest(q=q):
                self.assertIn("mobile", ts.needed_families(q))

    def test_image_generation_words(self):
        for q in ("make me a picture of a cat", "draw a logo for my startup"):
            with self.subTest(q=q):
                self.assertIn("image_gen", ts.needed_families(q))

    def test_single_words_match_on_boundaries(self):
        """"app" must not fire on "apple"; only the full phrase counts."""
        self.assertNotIn("mobile", ts.needed_families("an apple pie recipe"))
        self.assertNotIn("docs", ts.needed_families("update the docs/index.html"))
        self.assertIn("mobile", ts.needed_families("the app screen looks wrong"))

    def test_matching_is_case_insensitive(self):
        self.assertIn("browser", ts.needed_families("Use The BROWSER please"))

    def test_empty_and_junk_input_is_safe(self):
        for q in ("", "   ", None, 12345, {"not": "a string"}):
            with self.subTest(q=q):
                self.assertEqual(ts.needed_families(q), set())

    def test_flag_off_means_every_family(self):
        self.assertEqual(ts.needed_families("fix a test", {"main_lane_on_demand": False}),
                         set(ts.FAMILIES))

    def test_junk_config_never_raises(self):
        for cfg in (None, {}, "nonsense", {"keywords": "not-a-dict"},
                    {"keywords": {"browser": "not-a-list"}}, {"main_lane_on_demand": True}):
            with self.subTest(cfg=cfg):
                self.assertIsInstance(ts.needed_families("fix a test", cfg), set)


class TestFilter(unittest.TestCase):

    def test_core_tools_are_always_kept(self):
        names = ("read_file", "read_file_chunk", "write_file", "edit_file", "list_files",
                 "grep", "run_python", "run_shell", "create_plan", "get_plan",
                 "update_plan_item", "spawn_agent", "list_diff", "revert", "search_memory",
                 "web_search", "analyze_image")
        tools = [_tool(n) for n in names]
        kept = {t["function"]["name"] for t in ts.filter_tools(tools, "fix a test")}
        self.assertEqual(kept, set(names), "a coding run must keep every core tool")

    def test_situational_tools_are_withheld_then_restored(self):
        tools = [_tool(n) for n in ("read_file", "browser_click", "doc_inspect", "generate_image")]
        coding = {t["function"]["name"] for t in ts.filter_tools(tools, "fix a test")}
        self.assertEqual(coding, {"read_file"})
        for query, expected in (("open the website and click", "browser_click"),
                                ("edit this excel file", "doc_inspect"),
                                ("draw me a logo", "generate_image")):
            with self.subTest(query=query):
                kept = {t["function"]["name"] for t in ts.filter_tools(tools, query)}
                self.assertIn(expected, kept)

    def test_mcp_tools_are_never_hidden(self):
        tools = [_tool("mcp__acme__do_thing"), _tool("browser_click")]
        for q in ("fix a test", "open the website", "make me a picture"):
            with self.subTest(q=q):
                kept = {t["function"]["name"] for t in ts.filter_tools(tools, q)}
                self.assertIn("mcp__acme__do_thing", kept)

    def test_flag_off_returns_everything(self):
        tools = [_tool(n) for n in ("read_file", "browser_click", "doc_inspect", "mobile_tap")]
        self.assertEqual(len(ts.filter_tools(tools, "fix a test", {"main_lane_on_demand": False})),
                         len(tools))

    def test_unknown_and_malformed_entries_survive(self):
        tools = [{"function": {"name": "totally_new_tool"}}, {}, _tool("browser_click")]
        kept = [t for t in ts.filter_tools(tools, "fix a test") if "function" in t]
        self.assertIn("totally_new_tool", {t["function"]["name"] for t in kept})

    def test_empty_input(self):
        self.assertEqual(ts.filter_tools([], "fix a test"), [])
        self.assertEqual(ts.filter_tools(None, "fix a test"), [])

    def test_hidden_is_the_complement_of_needed(self):
        q = "open the website and take a screenshot"
        self.assertEqual(set(ts.hidden_families(q)),
                         set(ts.FAMILIES) - set(ts.needed_families(q)))
        self.assertEqual(ts.hidden_families(q), ["docs", "image_gen", "mobile"])


def _register_everything():
    """Mirror the app's startup tool registration (server_manager.py:192-199)."""
    from core.registry import bootstrap_builtin_tools
    from core.web_tools import register_web_tools
    from core.skills import register_skill_tools
    from core.shell_tools import register_shell_tools
    from core.file_tools import register_file_tools
    from core.media_tools import register_media_tools
    from core.browser_tools import register_browser_tools
    from core.device_tools import register_device_tools
    for fn in (bootstrap_builtin_tools, register_web_tools, register_skill_tools,
               register_shell_tools, register_file_tools, register_media_tools,
               register_browser_tools, register_device_tools):
        try:
            fn()
        except Exception:
            pass


class TestRealRegistrySaving(unittest.TestCase):
    """The token win, measured against the registry the app actually ships."""

    @classmethod
    def setUpClass(cls):
        _register_everything()
        from core.agent_loop import all_tools
        cls.tools = all_tools()

    @staticmethod
    def _schema_tokens(tools):
        return len(json.dumps(tools)) // 3

    def test_a_coding_request_sends_far_less_schema(self):
        core_only = ts.filter_tools(self.tools, "fix the failing test in core/db.py")
        full = len(self.tools)
        self.assertGreater(full, 20, "registry should hold the whole tool set")
        self.assertLess(len(core_only), full * 0.6,
                        "a coding run must not carry the situational families")
        self.assertLess(self._schema_tokens(core_only), self._schema_tokens(self.tools) * 0.6)

    def test_named_families_come_back(self):
        for query, tool in (("use the browser to click through the page", "browser_click"),
                            ("edit this excel spreadsheet", "doc_inspect"),
                            ("make me a picture of a cat", "generate_image"),
                            ("run it on my android emulator", "mobile_tap")):
            with self.subTest(query=query):
                kept = {t["function"]["name"] for t in ts.filter_tools(self.tools, query)}
                self.assertIn(tool, kept)

    def test_read_file_chunk_is_registered(self):
        """The system prompt orders every lane to page through big files with it, and
        the executor lane must therefore be offered it."""
        names = {t["function"]["name"] for t in self.tools}
        self.assertIn("read_file_chunk", names)


class TestWiring(unittest.TestCase):
    """Structural checks on routes/agent.py: the policy is actually applied."""

    @classmethod
    def setUpClass(cls):
        cls.src = AGENT_SRC.read_text(encoding="utf-8")

    def test_main_lane_goes_through_the_policy(self):
        self.assertIn("tools_for_lane = tool_surface.filter_tools(", self.src)

    def test_executor_lane_offers_read_file_chunk(self):
        start = self.src.index('elif lane_name == "executor":')
        block = self.src[start:self.src.index("if custom_agent_tools:", start)]
        self.assertIn('"read_file_chunk"', block)

    def test_escalated_call_uses_the_policy_too(self):
        self.assertIn("if req.plan else tool_surface.filter_tools(", self.src)

    def test_withheld_families_are_reported_to_the_ui(self):
        self.assertIn("hidden_fams = tool_surface.hidden_families(", self.src)
        self.assertIn("'hidden': hidden_fams", self.src)

    def test_custom_agent_allowlist_is_applied_after(self):
        """An explicit allowlist must still win over the policy."""
        at = self.src.index("tools_for_lane = tool_surface.filter_tools(")
        self.assertLess(at, self.src.index("if custom_agent_tools:", at))


class TestPerSlotCap(unittest.TestCase):
    """The small model now reuses core/process.py::per_slot_cap."""

    def test_cap_applies_only_with_several_slots_and_a_real_limit(self):
        from core.process import per_slot_cap
        base = {"kv_unified": True, "context_size": 32576, "n_slots": 3}
        self.assertEqual(per_slot_cap({**base, "kv_unified_per_slot": 0}), 0, "unset = unchanged")
        self.assertEqual(per_slot_cap({**base, "kv_unified_per_slot": 8192}), 8192)
        self.assertEqual(per_slot_cap({**base, "kv_unified_per_slot": 40000}), 0,
                         "a cap at or above the pool would only shrink the lane")
        self.assertEqual(per_slot_cap({**base, "kv_unified_per_slot": 8192, "n_slots": 1}), 0)
        self.assertEqual(per_slot_cap({**base, "kv_unified_per_slot": 8192, "kv_unified": False}), 0)

    def test_small_model_plumbs_the_key(self):
        src = "".join(p.read_text(encoding="utf-8")
                      for p in sorted((REPO / "core" / "small_model").glob("*.py")))
        self.assertIn('cfg.get("kv_unified_per_slot")', src)
        self.assertIn('"--kv-unified-per-slot"', src)


if __name__ == "__main__":
    unittest.main()
