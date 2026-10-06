"""Prompt-efficiency standard: intent detectors, tiering, budgets (S1-S5)."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import prompt_scope  # noqa: E402
from core.agent_loop import estimate_prompt_tokens  # noqa: E402


class FileIntentTests(unittest.TestCase):
    def test_plain_chat_has_no_file_intent(self):
        self.assertFalse(prompt_scope.file_intent("HI"))
        self.assertFalse(prompt_scope.file_intent("what is photosynthesis?"))
        self.assertFalse(prompt_scope.file_intent("thanks!"))

    def test_file_requests_detected(self):
        self.assertTrue(prompt_scope.file_intent("make me a resume.pdf"))
        self.assertTrue(prompt_scope.file_intent("create data.csv and share it"))
        self.assertTrue(prompt_scope.file_intent("export this to Excel"))

    def test_edit_followup_counts(self):
        self.assertTrue(prompt_scope.file_intent("make it red", file_followup="edit"))


class WebIntentTests(unittest.TestCase):
    def test_plain_chat_has_no_web_intent(self):
        self.assertFalse(prompt_scope.web_intent("HI"))
        self.assertFalse(prompt_scope.web_intent("what is photosynthesis?"))
        self.assertFalse(prompt_scope.web_intent("write a python loop"))

    def test_url_forces_web(self):
        self.assertTrue(prompt_scope.web_intent("summarize https://example.com/x",
                                                url_matches=["https://example.com/x"]))

    def test_recency_and_verbs(self):
        self.assertTrue(prompt_scope.web_intent("latest django version"))
        self.assertTrue(prompt_scope.web_intent("weather in Dhaka today"))
        self.assertTrue(prompt_scope.web_intent("search the web for pangolin photos"))

    def test_prior_research_keeps_web_on(self):
        msgs = [{"role": "assistant", "tool_calls": [
            {"id": "1", "function": {"name": "web_search", "arguments": "{}"}}]}]
        self.assertTrue(prompt_scope.web_intent("tell me more", prior_msgs=msgs))


class McpMentionTests(unittest.TestCase):
    NAMES = {"echo", "isms", "sequential-thinking", "time"}

    def test_no_mention_no_servers(self):
        self.assertEqual(prompt_scope.mcp_mentions("HI", self.NAMES), set())

    def test_mention_matches_whole_word(self):
        self.assertEqual(prompt_scope.mcp_mentions("ask isms to send it", self.NAMES), {"isms"})
        # "time" inside another word must not match
        self.assertEqual(prompt_scope.mcp_mentions("what is the runtime?", self.NAMES), set())
        self.assertEqual(prompt_scope.mcp_mentions("what time is it?", self.NAMES), {"time"})

    def test_prior_mcp_use_keeps_server(self):
        msgs = [{"role": "assistant", "tool_calls": [
            {"id": "1", "function": {"name": "mcp__time__get_current_time", "arguments": "{}"}}]}]
        self.assertEqual(prompt_scope.mcp_mentions("and now?", self.NAMES, msgs), {"time"})

    def test_unknown_names_ignored(self):
        self.assertEqual(prompt_scope.mcp_mentions("ask nosuch thing", {"nosuch"}), {"nosuch"})

    def test_schema_filter(self):
        schemas = [
            {"type": "function", "function": {"name": "mcp__time__get_current_time"}},
            {"type": "function", "function": {"name": "mcp__isms__send_sms"}},
            {"type": "function", "function": {"name": "write_file"}},
        ]
        got = prompt_scope.mcp_schemas_for(schemas, {"time"})
        self.assertEqual([g["function"]["name"] for g in got], ["mcp__time__get_current_time"])


class BudgetTests(unittest.TestCase):
    def _schemas(self):
        from core.agent_tools.schemas import CHAT_WRITE_FILE_SCHEMA
        from core.doc_tools.constants import DOC_EDIT_SCHEMA, DOC_INSPECT_SCHEMA
        from core.agent_tools import AGENT_TOOLS
        skb = next(t for t in AGENT_TOOLS
                   if t.get("function", {}).get("name") == "search_knowledge_base")
        return CHAT_WRITE_FILE_SCHEMA, DOC_INSPECT_SCHEMA, DOC_EDIT_SCHEMA, skb

    def test_moved_manuals_match_old_sizes(self):
        # guards the verbatim move from routes/chat/run.py: same text, same cost
        self.assertTrue(1000 <= len(prompt_scope.FILE_MANUAL) // 3 <= 1400,
                        len(prompt_scope.FILE_MANUAL) // 3)
        self.assertTrue(800 <= len(prompt_scope.WEB_MANUAL) // 3 <= 1100,
                        len(prompt_scope.WEB_MANUAL) // 3)
        self.assertLessEqual(len(prompt_scope.FILE_MANUAL_LEAN) // 3 + 1, 80)

    def test_lean_hi_stays_in_budget(self):
        """A trivial turn ships date + lean pointer + core tools only."""
        from routes.common.globals import current_date_prompt
        write, _, _, skb = self._schemas()
        sections = [("date", current_date_prompt()),
                    ("file-lean", prompt_scope.FILE_MANUAL_LEAN)]
        msgs = sections_as_msgs(sections) + [{"role": "user", "content": "HI"}]
        total = estimate_prompt_tokens(msgs, [write, skb])
        self.assertLessEqual(total, prompt_scope.CHAT_LEAN_BUDGET, total)

    def test_full_intent_keeps_manuals(self):
        """File/web intent restores the full manuals (no behavior loss)."""
        write, inspect, edit, skb = self._schemas()
        sections = [("file-manual", prompt_scope.FILE_MANUAL),
                    ("web-manual", prompt_scope.WEB_MANUAL)]
        msgs = sections_as_msgs(sections) + [{"role": "user", "content": "make a pdf of latest news"}]
        total = estimate_prompt_tokens(msgs, [write, inspect, edit, skb])
        self.assertGreater(total, prompt_scope.CHAT_LEAN_BUDGET)
        self.assertIn("write_file", prompt_scope.FILE_MANUAL)
        self.assertIn("web_search", prompt_scope.WEB_MANUAL)

    def test_breakdown_sums_parts(self):
        b = prompt_scope.breakdown([("a", "hello world"), ("b", "foo")], [{"x": 1}])
        self.assertEqual(b["total"], sum(b["parts"].values()))
        self.assertIn("tools", b["parts"])

    def test_logging_is_off_by_default(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("PROMPT_BREAKDOWN", None)
            with mock.patch.object(prompt_scope, "scope_logging_enabled", return_value=False):
                with mock.patch("builtins.print") as p:
                    prompt_scope.maybe_log("t", [("a", "b")], [])
                p.assert_not_called()


def sections_as_msgs(sections):
    return [{"role": "system", "content": t} for _, t in sections]


class AgentPromptTests(unittest.TestCase):
    def test_system_prompt_stays_in_budget(self):
        from core.agent_loop.prompts import AGENT_SYSTEM_PROMPT
        out = AGENT_SYSTEM_PROMPT.format(workspace="W")
        total = estimate_prompt_tokens([{"role": "system", "content": out}])
        self.assertLessEqual(total, prompt_scope.AGENT_SYSTEM_BUDGET, total)

    def test_trim_kept_every_capability(self):
        from core.agent_loop.prompts import AGENT_SYSTEM_PROMPT
        out = AGENT_SYSTEM_PROMPT.format(workspace="W")
        for keep in ("read_file_chunk", "revert", "spawn_agent",
                     "spawn_parallel_agents", "create_plan", "update_plan_item",
                     "memory_append", "memory_str_replace", "memory_delete",
                     "finish(answer)", "run_shell", "doc_create", "grep",
                     "append_file", "read_skill", '{"op":"set_text"'):
            self.assertIn(keep, out, keep)

    def test_agent_mcp_server_selection(self):
        self.assertEqual(prompt_scope.agent_mcp_servers({"time"}, None), {"time"})
        self.assertEqual(prompt_scope.agent_mcp_servers(set(), ["mcp__isms__send_sms"]),
                         {"isms"})
        self.assertEqual(prompt_scope.agent_mcp_servers({"time"}, ["mcp__isms__send_sms"]),
                         {"time", "isms"})

    def test_hide_unmentioned_mcp(self):
        tools = [
            {"type": "function", "function": {"name": "mcp__time__now"}},
            {"type": "function", "function": {"name": "mcp__isms__send"}},
            {"type": "function", "function": {"name": "write_file"}},
        ]
        got = prompt_scope.hide_unmentioned_mcp(tools, {"time"})
        self.assertEqual([t["function"]["name"] for t in got],
                         ["mcp__time__now", "write_file"])
        # everything mentioned: nothing hidden
        self.assertEqual(len(prompt_scope.hide_unmentioned_mcp(tools, {"time", "isms"})), 3)

    def test_router_only_sees_plan_tools(self):
        """The router may only short-cut plan tools, so it must only be
        shown those (not the full registry on every step)."""
        from routes.agent.constants import PLAN_MODE_TOOLS
        # the loop body moved to stream.py (2026-10-06); scan both modules
        src = "\n".join(p.read_text(encoding="utf-8")
                        for p in (Path("routes/agent/run.py"),
                                  Path("routes/agent/stream.py")))
        i = src.index("r_tools = [t for t in all_tools()")
        self.assertIn("PLAN_MODE_TOOLS", src[i:i + 200])
        self.assertGreaterEqual(len(PLAN_MODE_TOOLS), 10)

    def test_agent_mcp_wiring(self):
        setup = Path("routes/agent/setup.py").read_text(encoding="utf-8")
        self.assertIn("ctx.mcp_servers = prompt_scope.mcp_mentions", setup)
        run = "\n".join(p.read_text(encoding="utf-8")
                        for p in (Path("routes/agent/run.py"),
                                  Path("routes/agent/stream.py")))
        self.assertIn("prompt_scope.hide_unmentioned_mcp", run)


if __name__ == "__main__":
    unittest.main()
