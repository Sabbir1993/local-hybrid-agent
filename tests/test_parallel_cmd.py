"""/parallel <task> always fans out: the first main-lane step offers only spawn_parallel_agents with a call required,
for at most MAX_FORCED_STEPS steps, and never in the Personal Agent (which blocks the spawn tools)."""
import unittest
from pathlib import Path

from core.agent_loop import parallel_cmd


class Parse(unittest.TestCase):
    def test_command_is_recognised_and_stripped(self):
        self.assertEqual(parallel_cmd.parse("/parallel review core and routes"), (True, "review core and routes"))
        self.assertEqual(parallel_cmd.parse("  /PARALLEL\nreview it"), (True, "review it"))

    def test_other_text_is_left_alone(self):
        for t in ("review /parallel later", "/parallelize x", "hello", None, ["x"]):
            self.assertFalse(parallel_cmd.parse(t)[0], t)

    def test_instruction_names_the_tool_and_keeps_the_task(self):
        msg = parallel_cmd.instruction("audit the auth module")
        self.assertIn("spawn_parallel_agents", msg)
        self.assertTrue(msg.endswith("Task: audit the auth module"))


class ForcedStep(unittest.TestCase):
    def test_forced_until_the_fan_out_happens(self):
        self.assertTrue(parallel_cmd.wants_forced_step(True, [], 0))
        self.assertTrue(parallel_cmd.wants_forced_step(True, [{"name": "read_file"}], 1))
        self.assertFalse(parallel_cmd.wants_forced_step(True, [{"name": "spawn_parallel_agents"}], 1))

    def test_gives_up_after_the_cap_and_never_when_off(self):
        self.assertFalse(parallel_cmd.wants_forced_step(True, [], parallel_cmd.MAX_FORCED_STEPS))
        self.assertFalse(parallel_cmd.wants_forced_step(False, [], 0))


class Wiring(unittest.TestCase):
    """Source checks, like test_project_builder: the loop is too large to drive here."""
    SRC = (Path(__file__).resolve().parents[1] / "routes" / "agent" / "stream.py").read_text(encoding="utf-8")
    SETUP = (Path(__file__).resolve().parents[1] / "routes" / "agent" / "setup.py").read_text(encoding="utf-8")

    def test_forced_step_offers_one_tool_and_requires_a_call(self):
        self.assertIn('tools_for_lane, step_tool_choice, step_grammar = _par_tools, "required", None', self.SRC)
        self.assertIn("parallel_cmd.TOOL not in (hidden_tools or ())", self.SRC)

    def test_a_run_that_may_not_spawn_is_told_why(self):
        self.assertIn("ctx.parallel = not ctx.parallel_note", self.SETUP)


class AgentModeSwitch(unittest.TestCase):
    """agent.personal_subagents: off by default; on, an Agent-mode child keeps every Personal Agent rule and adds its own."""

    def setUp(self):
        from core import request_context as rc
        from core.small_model import APP_CONFIG
        self.rc, self.cfg = rc, APP_CONFIG
        self._saved = dict(APP_CONFIG.get("agent") or {})
        self.addCleanup(lambda: APP_CONFIG.__setitem__("agent", self._saved))
        tok = rc.set_personal_scope(True)
        self.addCleanup(rc._personal_scope.reset, tok)

    def flag(self, on):
        self.cfg["agent"] = {**self._saved, "personal_subagents": on}

    def test_off_by_default_and_the_user_is_told(self):
        self.cfg["agent"] = {k: v for k, v in self._saved.items() if k != "personal_subagents"}
        self.assertIn("spawn_parallel_agents", self.rc.personal_blocked_tools())
        why = parallel_cmd.unavailable_reason(True, self.rc.personal_blocked_tools(), None)
        self.assertIn("Agent limits", why)

    def test_on_unblocks_only_the_spawn_tools(self):
        self.flag(True)
        blocked = self.rc.personal_blocked_tools()
        self.assertNotIn("spawn_parallel_agents", blocked)
        self.assertIn("git_commit", blocked)
        self.assertEqual(parallel_cmd.unavailable_reason(True, blocked, None), "")

    def test_agent_without_spawn_in_its_tool_list_is_told(self):
        self.flag(True)
        self.assertIn("Spawn Sub-Agent", parallel_cmd.unavailable_reason(True, frozenset(), {"read_file"}))

    def test_child_tool_set_drops_blocked_browser_and_phone_tools(self):
        from core.subagent.runner import resolve_subagent_tools
        self.flag(True)
        got = resolve_subagent_tools({"read_file", "write_file", "git_commit", "browser_click", "mobile_tap", "run_shell"})
        self.assertEqual(got, {"read_file", "write_file"})

    def test_child_refuses_what_the_parent_would_ask_about(self):
        from core.subagent.runner import personal_child_refusal
        tok = self.rc.set_permission_mode("manual")
        self.addCleanup(self.rc._permission_mode.reset, tok)
        self.assertIn("approval", personal_child_refusal("write_file", {"path": "a.md"}))
        self.assertIn("approval", personal_child_refusal("mcp__gmail__send", {}))
        self.assertIsNone(personal_child_refusal("read_file", {"path": "a.md"}))
        self.rc.set_permission_mode("auto")
        self.assertIsNone(personal_child_refusal("write_file", {"path": "a.md"}))

    def test_code_mode_children_are_unchanged(self):
        from core.subagent.runner import personal_child_refusal
        self.rc.set_personal_scope(False)
        self.assertIsNone(personal_child_refusal("browser_click", {}))


if __name__ == "__main__":
    unittest.main()
