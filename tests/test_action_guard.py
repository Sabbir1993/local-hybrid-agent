"""tests/test_action_guard.py - planning / research steps with no action: nudge, withhold tools, stop.

Run: python -m unittest tests.test_action_guard -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.agent_loop import action_guard as A
from core.agent_loop.policy import describe_stop

PASSIVE = ["web_fetch"]


class ActionGuardTests(unittest.TestCase):
    def run_steps(self, g, n, names=PASSIVE, start=0):
        return [g.record_step(names, start + i) for i in range(n)]

    def test_nudge_then_ban_then_stop(self):
        g = A.ActionGuard()
        out = self.run_steps(g, A.STOP_AT)
        self.assertEqual(out[A.NUDGE_AT - 1], "nudge")
        self.assertEqual(out[A.BAN_AT - 1], "ban")
        self.assertEqual(out[-1], "stop")
        self.assertEqual([o for o in out[:A.NUDGE_AT - 1]], [""] * (A.NUDGE_AT - 1))

    def test_any_action_resets_the_streak(self):
        g = A.ActionGuard()
        self.run_steps(g, 2)
        self.assertEqual(g.record_step(["web_fetch", "write_file"], 2), "")
        self.assertEqual(g.streak, 0)

    def test_a_step_with_no_tools_is_not_passive(self):
        g = A.ActionGuard()
        self.run_steps(g, 2)
        self.assertEqual(g.record_step([], 2), "")
        self.assertEqual(g.streak, 0)

    def test_the_ban_covers_the_next_steps_only(self):
        g = A.ActionGuard()
        for i in range(A.BAN_AT):
            g.record_step(PASSIVE, i)                 # the 5th step (index 4) triggers the ban
        first = A.BAN_AT                               # next step index
        self.assertTrue(g.banned(first))
        self.assertTrue(g.banned(first + A.BAN_STEPS - 1))
        self.assertFalse(g.banned(first + A.BAN_STEPS))

    def test_plan_and_research_are_passive_but_writes_and_shell_are_not(self):
        for n in ("create_plan", "web_search", "read_file", "grep"):
            self.assertIn(n, A.PASSIVE_TOOLS)
        for n in ("write_file", "edit_file", "run_shell", "run_python", "spawn_agent", "update_plan_item"):
            self.assertNotIn(n, A.PASSIVE_TOOLS)

    def test_messages_name_the_step_and_the_action(self):
        m = A.nudge_message(3, "Initialize Vite project")
        self.assertIn("Initialize Vite project", m)
        self.assertIn("run_shell", m)
        self.assertIn("create_plan", m)

    def test_stop_reads_in_plain_language(self):
        self.assertIn("only planning and looking things up", describe_stop("no_action:steps:8"))


class Wiring(unittest.TestCase):
    src = Path("routes/agent/stream.py").read_text(encoding="utf-8")

    def test_loop_uses_the_guard(self):
        for needle in ("action_guard.record_step(", "action_guard.banned(step)", "_act_forced", ":steps:{action_guard.streak}",
                       "[plan recorded]"):
            self.assertIn(needle, self.src, needle)


if __name__ == "__main__":
    unittest.main()
