"""A skill name is a valid spawn_agent role, and the sub-agent budget is real.

Two fixes are pinned here.

1. `webapp-testing` is a SKILL, not a role, but both are advertised in the same
   system prompt - so models pass one as the other and got
   "unknown role 'webapp-testing'". core/roles.py now falls back to skills.

2. The sub-agent compacted against a hardcoded `ex_ctx = 16384` and called
   compact_messages() WITHOUT `tools=`, so the ~8.5k-token tool-schema block was
   invisible to the estimator. The estimate said "fits"; llama.cpp refused. The
   window now comes from the lane that was actually chosen (core/subagent.py).

Run: python -m unittest tests.test_subagent_roles -v
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import context_budget  # noqa: E402
from core import subagent  # noqa: E402
from core.roles import known_role_names, resolve_role  # noqa: E402


class SkillAsRoleTests(unittest.TestCase):
    def test_a_skill_name_resolves_as_a_role(self):
        cfg = resolve_role("webapp-testing")
        self.assertTrue(cfg, "webapp-testing is an installed skill and must resolve")
        self.assertTrue(cfg.get("is_skill"))
        self.assertIsNone(cfg["lane"], "a skill must not pin a lane; the job mapping decides")
        self.assertIsNone(cfg.get("tools"), "the skill's own steps choose their tools")
        self.assertIn("webapp-testing", cfg["system_prompt"])
        self.assertGreater(len(cfg["system_prompt"]), 100, "the skill body must be injected")

    def test_skill_names_are_advertised_as_roles(self):
        self.assertIn("webapp-testing", known_role_names())

    def test_an_unknown_name_still_fails_closed(self):
        self.assertEqual(resolve_role("definitely-not-a-real-thing"), {})

    def test_a_real_role_is_unaffected(self):
        self.assertTrue(resolve_role("reviewer"), "built-in roles must still resolve")
        self.assertFalse(resolve_role("reviewer").get("is_skill"))

    def test_no_name_is_harmless(self):
        self.assertEqual(resolve_role(None), {})
        self.assertEqual(resolve_role(""), {})

    def test_a_long_skill_body_is_capped(self):
        """A skill body must not be able to eat the sub-agent's whole window."""
        from core.roles import MAX_ROLE_SKILL_CHARS, _resolve_skill
        huge = {"webapp-testing": {"name": "webapp-testing", "description": "x",
                                   "body": "y" * (MAX_ROLE_SKILL_CHARS * 3)}}
        with mock.patch("core.skills.load_skills", return_value=huge):
            cfg = _resolve_skill("webapp-testing")
        self.assertIn("truncated", cfg["system_prompt"])
        self.assertLess(len(cfg["system_prompt"]), MAX_ROLE_SKILL_CHARS * 2)

    def test_a_skill_lookup_failure_is_not_fatal(self):
        with mock.patch("core.skills.load_skills", side_effect=OSError("no skills dir")):
            self.assertEqual(resolve_role("webapp-testing"), {})


class _Target:
    """A lanes.Target stand-in: the lane name plus the instance behind it."""

    def __init__(self, inst=None, lane="executor", cm=None):
        self.lane, self.inst, self.cm = lane, inst, cm


class _Inst:
    def __init__(self, ctx, np):
        self.cfg = {"ctx": ctx, "np": np}
        self.client = None


class SubagentWindowTests(unittest.TestCase):
    """_subagent_window must reflect the lane the sub-agent actually landed on."""

    def setUp(self):
        for lane in ("executor", "main"):
            context_budget._state(lane).update(
                {"window": 0, "probed_at": 0, "probe_failed_at": 0, "factor": 1.0})

    def test_config_ctx_is_used_when_nothing_is_probing(self):
        t = _Target(_Inst(132576, 1))
        win = asyncio.run(subagent._subagent_window(t))
        self.assertGreaterEqual(win, 132576,
                                "a 132k configured executor must not be budgeted at 16k")

    def test_parallel_slots_keep_the_whole_pool(self):
        """-np with a unified KV pool (-kvu) still gives each slot the full -c.

        Mirrors routes/agent.py::_lane_window, which passes kv_unified=n_slots > 1
        for the same reason: a unified pool is shared, not divided.
        """
        win = asyncio.run(subagent._subagent_window(_Target(_Inst(132576, 4))))
        self.assertGreaterEqual(win, 132576,
                                "a unified pool must not be divided by the slot count")

    def test_a_slots_probe_overrides_the_config(self):
        """/slots n_ctx is authoritative - it is what the server enforces."""
        t = _Target(_Inst(132576, 1))
        with mock.patch.object(context_budget, "probe_window",
                               new=mock.AsyncMock(return_value=8192)):
            win = asyncio.run(subagent._subagent_window(t))
        self.assertEqual(win, 8192)

    def test_a_cloud_lane_uses_the_provider_ctx(self):
        class _CM:
            ctx = 200000
        win = asyncio.run(subagent._subagent_window(_Target(None, cm=_CM())))
        self.assertGreaterEqual(win, 200000)

    def test_an_unconfigured_lane_falls_back_rather_than_returning_zero(self):
        win = asyncio.run(subagent._subagent_window(_Target(None)))
        self.assertEqual(win, subagent.SUBAGENT_FALLBACK_CTX,
                         "a 0 budget would skip compaction entirely and overflow")


class SubagentBudgetTests(unittest.TestCase):
    """The budget must count tool schemas, or the estimate lies to the server."""

    @staticmethod
    def _tools(n=40):
        return [{"type": "function", "function": {"name": f"t{i}", "description": "d" * 200}}
                for i in range(n)]

    def test_tools_are_charged_against_the_budget(self):
        from core.agent_loop import estimate_prompt_tokens
        msgs = [{"role": "system", "content": "S" * 4000}, {"role": "user", "content": "go"}]
        self.assertGreater(estimate_prompt_tokens(msgs, self._tools()),
                           estimate_prompt_tokens(msgs) + 2000,
                           "the schema block must be visible to the estimator")

    def test_compaction_leaves_the_prompt_under_budget(self):
        from core.agent_loop import compact_messages, estimate_prompt_tokens
        tools = self._tools()
        msgs = [{"role": "system", "content": "S" * 4000}]
        for i in range(20):
            msgs.append({"role": "assistant", "content": "",
                         "tool_calls": [{"id": f"c{i}", "type": "function",
                                         "function": {"name": "read_file", "arguments": "{}"}}]})
            msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "x" * 20000})
        out = compact_messages(msgs, 16000, tools=tools)
        self.assertLess(estimate_prompt_tokens(out, tools), 16000,
                        "the result must fit the window it was given, schemas included")


if __name__ == "__main__":
    unittest.main()
