"""The name-only near-repeat stop is opt-in: a code review that runs many different shell commands
must not be stopped, while real repeats are still caught by the other guards."""
import re
import unittest
from pathlib import Path

from core.agent_loop.loop_guard import LoopGuard


class NearRepeatOffTests(unittest.TestCase):
    def test_run_gates_the_rule_on_config(self):
        # loop body moved to stream.py (2026-10-06); scan both modules
        src = "\n".join(p.read_text(encoding="utf-8")
                        for p in (Path("routes/agent/run.py"),
                                  Path("routes/agent/stream.py")))
        self.assertRegex(src, r"near_repeat_on = bool\(.*\"near_repeat\", False\)")
        self.assertIn("if sigs and near_repeat_on:", src)

    def test_many_different_shell_calls_do_not_trip_the_guard(self):
        g = LoopGuard()
        for i in range(40):
            g.record("run_shell", {"command": f"type src\file{i}.js"}, f"contents of file {i}", ok=True)
            self.assertEqual(g.check().level, "ok", f"call {i}")

    def test_identical_repeats_are_still_caught(self):
        g = LoopGuard()
        actions = []
        for _ in range(4):
            g.record("run_shell", {"command": "dir"}, "same output", ok=True)
            actions.append(g.check().level)
        self.assertNotEqual(set(actions), {"ok"})


if __name__ == "__main__":
    unittest.main()
