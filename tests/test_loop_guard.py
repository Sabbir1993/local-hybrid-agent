import unittest

from core.agent_loop import loop_guard as lg


def feed(guard, calls):
    """calls: (name, args, result, ok). Returns the verdict after each one."""
    out = []
    for name, args, result, ok in calls:
        guard.record(name, args, result, ok)
        out.append(guard.check())
    return out


class ProgressIsNeverALoop(unittest.TestCase):
    def test_long_debugging_session_with_new_results_is_fine(self):
        g = lg.LoopGuard()
        calls = [("run_shell", {"cmd": f"cmd {i}"}, f"output {i}", True) for i in range(40)]
        self.assertTrue(all(v.level == lg.OK for v in feed(g, calls)))

    def test_same_tool_many_different_files_is_fine(self):
        g = lg.LoopGuard()
        calls = [("read_file", {"path": f"f{i}.py"}, f"contents of file {i}", True) for i in range(60)]
        self.assertTrue(all(v.level == lg.OK for v in feed(g, calls)))

    def test_rerunning_the_same_command_after_changes_is_progress(self):
        g = lg.LoopGuard()
        calls = [("run_shell", {"cmd": "npm test"}, "3 failed", True),
                 ("edit_file", {"path": "a.js"}, "edited", True),
                 ("run_shell", {"cmd": "npm test"}, "1 failed", True),
                 ("edit_file", {"path": "b.js"}, "edited b", True),
                 ("run_shell", {"cmd": "npm test"}, "all passed", True)]
        self.assertTrue(all(v.level == lg.OK for v in feed(g, calls)))

    def test_whitespace_only_differences_are_still_the_same_result(self):
        g = lg.LoopGuard()
        vs = feed(g, [("run_shell", {"cmd": "x"}, "a  b\n c", True),
                      ("run_shell", {"cmd": "x"}, "a b c", True),
                      ("run_shell", {"cmd": "x"}, " a b   c ", True)])
        self.assertEqual(vs[-1].level, lg.NUDGE)


class Detection(unittest.TestCase):
    def test_identical_result_repeated(self):
        g = lg.LoopGuard()
        vs = feed(g, [("run_python", {"code": "print(1)"}, "1", True)] * 3)
        self.assertEqual([v.level for v in vs], [lg.OK, lg.OK, lg.NUDGE])
        self.assertEqual((vs[-1].rule, vs[-1].tool, vs[-1].count), ("identical_result", "run_python", 3))
        self.assertEqual(vs[-1].detail, "identical_result:run_python:3")

    def test_error_streak(self):
        g = lg.LoopGuard()
        vs = feed(g, [("run_shell", {"cmd": f"c{i}"}, f"error: nope {i}", False) for i in range(6)])
        self.assertEqual(vs[-1].rule, "error_streak")
        self.assertTrue(all(v.level == lg.OK for v in vs[:-1]))

    def test_one_success_breaks_the_error_streak(self):
        g = lg.LoopGuard()
        calls = [("run_shell", {"cmd": f"c{i}"}, f"error: {i}", False) for i in range(5)]
        calls.append(("read_file", {"path": "a"}, "fine", True))
        calls.append(("run_shell", {"cmd": "c9"}, "error: 9", False))
        self.assertTrue(all(v.level == lg.OK for v in feed(g, calls)))

    def test_cycle_between_two_calls(self):
        g = lg.LoopGuard()
        a = ("run_shell", {"cmd": "build"}, "failed: x", True)
        b = ("edit_file", {"path": "a"}, "edited", True)
        vs = feed(g, [a, b] * 2)
        self.assertEqual([v.level for v in vs], [lg.OK, lg.OK, lg.OK, lg.NUDGE])
        self.assertEqual((vs[3].rule, vs[3].tool), ("cycle", "run_shell"))

    def test_disabled(self):
        g = lg.LoopGuard({"enabled": False})
        vs = feed(g, [("run_python", {"code": "1"}, "1", True)] * 10)
        self.assertTrue(all(v.level == lg.OK for v in vs))


class GraduatedResponse(unittest.TestCase):
    def _stuck(self, g, n):
        return feed(g, [("run_python", {"code": "print(1)"}, "1", True)] * n)

    def test_default_never_stops_the_run(self):
        g = lg.LoopGuard()
        levels = [v.level for v in self._stuck(g, 30)]
        self.assertNotIn(lg.STOP, levels)
        self.assertIn(lg.ESCALATE, levels)

    def test_nudge_then_escalate_then_stop_with_grace_between(self):
        g = lg.LoopGuard({"stop": True})
        levels = [v.level for v in self._stuck(g, 3 + 3 + 3)]
        # calls 1-2 ok, 3 nudge, 4-5 grace, 6 escalate, 7-8 grace, 9 stop
        self.assertEqual(levels, [lg.OK, lg.OK, lg.NUDGE, lg.OK, lg.OK, lg.ESCALATE, lg.OK, lg.OK, lg.STOP])

    def test_non_graduated_stops_at_the_first_trigger(self):
        g = lg.LoopGuard({"graduated": False, "stop": True})
        self.assertEqual(self._stuck(g, 3)[-1].level, lg.STOP)

    def test_thresholds_come_from_config(self):
        g = lg.LoopGuard({"identical_result": 5})
        self.assertEqual([v.level for v in self._stuck(g, 5)][-2:], [lg.OK, lg.NUDGE])
        self.assertEqual(lg.config({"identical_result": None, "unknown": 1})["identical_result"], 3)

    def test_model_that_reacts_to_the_nudge_keeps_going(self):
        g = lg.LoopGuard()
        self._stuck(g, 3)                                           # nudge
        vs = feed(g, [("run_shell", {"cmd": f"new {i}"}, f"new output {i}", True) for i in range(6)])
        self.assertTrue(all(v.level == lg.OK for v in vs))
        self.assertEqual(g.triggers, 1)


class Messages(unittest.TestCase):
    def test_text_names_the_tool_and_the_count(self):
        v = lg.Verdict(lg.NUDGE, "identical_result", "run_python", 3)
        msg = lg.nudge_message(v)
        self.assertTrue(msg.startswith("[stuck]"))
        self.assertIn("`run_python` was called 3 times", msg)
        self.assertIn("all failed", lg.describe("error_streak", "x", 6))
        self.assertIn("back and forth", lg.describe("cycle", "run_shell", 3))


class RecordedRun(unittest.TestCase):
    def test_the_28_step_debugging_run_that_used_to_stop_is_not_a_loop(self):
        """Tool sequence of run 79e01f21 (run_shell x9, run_python x4, read_file x4, browser calls...),
        each call with its own result: real work, not a loop."""
        seq = ["run_shell", "browser_navigate", "browser_console"] + ["run_shell"] * 9 + \
              ["browser_navigate", "browser_console", "browser_console", "run_shell", "run_shell", "run_python",
               "run_shell", "browser_navigate", "browser_console", "browser_screenshot", "read_file", "read_file",
               "read_file", "read_file", "run_shell", "run_python", "run_python", "run_python", "run_python"]
        g = lg.LoopGuard()
        vs = feed(g, [(n, {"i": i}, f"result {i}", True) for i, n in enumerate(seq)])
        self.assertTrue(all(v.level == lg.OK for v in vs))


if __name__ == "__main__":
    unittest.main()
