"""tests/test_policy.py - unit tests for the pure orchestration policy.

Run: python -m unittest tests.test_policy -v

core/agent_loop/policy.py holds the lane decision and the stop-summary text that used to
live as nested ternaries and hand-synchronized dicts inside routes/agent/run.py. No I/O,
no routes imports, no server: every case below runs in microseconds.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.agent_loop.policy import (
    Lane,
    LaneInputs,
    StopContext,
    StopReason,
    decide_lane,
    describe_stop,
    parallel_spawn_eligible,
    stop_headline,
    stop_note,
)


class DecideLane(unittest.TestCase):
    def test_default_is_executor(self):
        lane, reason = decide_lane(LaneInputs())
        self.assertEqual((lane, reason), (Lane.EXECUTOR, "executor_default"))

    def test_no_executor_means_main(self):
        lane, reason = decide_lane(LaneInputs(use_executor=False))
        self.assertEqual((lane, reason), (Lane.MAIN, "no_executor"))

    def test_stuck_executor_escalates(self):
        lane, reason = decide_lane(LaneInputs(executor_stuck=True))
        self.assertEqual((lane, reason), (Lane.MAIN, "repeat_streak"))

    def test_main_first(self):
        lane, reason = decide_lane(LaneInputs(main_first=True, main_first_why="plan_first_main"))
        self.assertEqual((lane, reason), (Lane.MAIN, "plan_first_main"))

    def test_forced_main_carries_its_reason(self):
        lane, reason = decide_lane(LaneInputs(forced_main=True, force_main_why="loop_guard"))
        self.assertEqual((lane, reason), (Lane.MAIN, "loop_guard"))

    def test_sustained_escalation_names_the_cause(self):
        lane, reason = decide_lane(LaneInputs(esc_sustained=True, esc_streak_reason="repeat"))
        self.assertEqual((lane, reason), (Lane.MAIN, "escalated_repeat"))

    def test_plan_first(self):
        lane, reason = decide_lane(LaneInputs(plan_first=True))
        self.assertEqual((lane, reason), (Lane.MAIN, "plan_first"))

    def test_plan_first_loses_to_forced_main_in_the_reason(self):
        # Transcribed quirk, verified against the original ternary: the plan_first arm is
        # guarded by `not forced_main`, so when both flags are set the reason falls through
        # to force_main_why. The lane is main either way.
        lane, reason = decide_lane(LaneInputs(plan_first=True, forced_main=True,
                                              force_main_why="loop_guard"))
        self.assertEqual(lane, Lane.MAIN)
        self.assertEqual(reason, "loop_guard")

    def test_plan_first_reason_needs_clean_guards(self):
        # plan_first with any other flag set falls through to that flag's reason
        _, reason = decide_lane(LaneInputs(plan_first=True, main_first=True,
                                           main_first_why="m"))
        self.assertEqual(reason, "m")

    def test_lane_is_a_str_enum(self):
        # the route compares lane_name == "executor", interpolates it into f-strings and JSON.
        # A non-str enum would silently change all three.
        import json
        lane, _ = decide_lane(LaneInputs())
        self.assertEqual(lane, "executor")
        self.assertIsInstance(lane, str)
        self.assertEqual(json.dumps({"lane": Lane.MAIN}), '{"lane": "main"}')


class StopReasons(unittest.TestCase):
    def test_every_member_has_note_and_headline(self):
        ctx = StopContext(steps=100, steps_run=12, run_limit_s=2700,
                          run_token_budget=2500000, run_prompt_tokens=1234567,
                          loop_detail="identical_result:run_python:3", elapsed_s=65)
        for reason in StopReason:
            with self.subTest(reason=reason.value):
                note = stop_note(reason.value, ctx)
                head = stop_headline(reason.value, ctx)
                self.assertTrue(note and note != "stopped",
                                f"{reason.value} has no note text")
                self.assertTrue(head and head != "Stopped",
                                f"{reason.value} has no headline text")

    def test_unknown_reason_falls_back(self):
        ctx = StopContext()
        self.assertEqual(stop_note("answered", ctx), "stopped")
        self.assertEqual(stop_headline("answered", ctx), "Stopped")

    def test_numbers_are_interpolated(self):
        ctx = StopContext(steps=100, run_limit_s=2700, run_token_budget=2500000,
                          run_prompt_tokens=1234567)
        self.assertIn("100", stop_note("max_steps", ctx))
        self.assertIn("2700", stop_note("timeout", ctx))
        self.assertIn("2,500,000", stop_note("budget", ctx))
        self.assertIn("1,234,567", stop_headline("budget", ctx))

    def test_loop_detail_names_the_tool(self):
        ctx = StopContext(loop_detail="read_file")
        self.assertIn("read_file", stop_note("loop_near_repeat", ctx))
        self.assertIn("read_file", stop_headline("loop_near_repeat", ctx))

    def test_no_progress_uses_the_guard_wording(self):
        ctx = StopContext(loop_detail="identical_result:run_python:3")
        self.assertIn("run_python", stop_note("no_progress", ctx))
        self.assertIn("run_python", stop_headline("no_progress", ctx))

    def test_banner_summary_and_wrapup_agree(self):
        # replaces the old source-grep test that asserted both dicts exist in run.py:
        # the note (banner) and headline (wrap-up summary) must describe the same stop.
        # Tokens are chosen per reason to appear in BOTH texts (the two use different
        # numbers: the note quotes the budget/limit, the headline the actual usage).
        ctx = StopContext(steps=100, steps_run=100, run_limit_s=2700,
                          run_token_budget=2500000, run_prompt_tokens=2500000,
                          loop_detail="identical_result:run_python:3", elapsed_s=2700)
        for reason, token in (("max_steps", "100"), ("timeout", "wall-clock"),
                              ("budget", "token budget"), ("loop", "repeat"),
                              ("loop_near_repeat", "repeat"),
                              ("no_progress", "run_python")):
            with self.subTest(reason=reason):
                note, head = stop_note(reason, ctx), stop_headline(reason, ctx)
                self.assertIn(token, note)
                self.assertIn(token, head)


class DescribeStop(unittest.TestCase):
    def test_guard_wording_passthrough(self):
        from core.agent_loop import loop_guard as lg
        v = lg.Verdict(lg.STOP, "identical_result", "run_python", 3)
        self.assertEqual(describe_stop(v.detail), lg.describe("identical_result", "run_python", 3))

    def test_garbage_falls_back(self):
        self.assertEqual(describe_stop("garbage"), "the run repeated itself")
        self.assertEqual(describe_stop(""), "the run repeated itself")
        self.assertEqual(describe_stop(None), "the run repeated itself")


class ParallelSpawnEligible(unittest.TestCase):
    """D4: the parallel spawn_agent fan-out in routes/agent/run.py skips the
    sequential gauntlet's plan-mode / plan-first gates, so a call those gates
    would stop must not take the fast path -- it falls through to the
    sequential loop, which reports the error. spawn_agent is never a read-only
    tool, so under either gate it is ineligible; read-only tools stay eligible."""

    READ_ONLY = frozenset({"read_file", "list_files", "grep", "create_plan"})

    def test_spawn_agent_ineligible_under_plan_mode(self):
        self.assertFalse(parallel_spawn_eligible("spawn_agent", True, self.READ_ONLY))

    def test_spawn_agent_ineligible_under_plan_gate(self):
        self.assertFalse(parallel_spawn_eligible("spawn_agent", True, self.READ_ONLY))

    def test_spawn_agent_eligible_when_unrestricted(self):
        self.assertTrue(parallel_spawn_eligible("spawn_agent", False, self.READ_ONLY))

    def test_read_only_tool_stays_eligible_under_plan_mode(self):
        self.assertTrue(parallel_spawn_eligible("read_file", True, self.READ_ONLY))

    def test_unknown_tool_takes_the_gauntlet_under_plan_mode(self):
        self.assertFalse(parallel_spawn_eligible("run_shell", True, self.READ_ONLY))


class ParallelSpawnWiring(unittest.TestCase):
    """The route must actually consult parallel_spawn_eligible on the parallel
    fan-out (both the card pre-emit and the dispatch filter) -- the predicate
    alone enforces nothing if run.py stops calling it."""

    @classmethod
    def setUpClass(cls):
        # the fan-out loops moved verbatim to stream.py (2026-10-06); the guard
        # is about the agent call path, so scan both modules
        cls.src = "\n".join(
            p.read_text(encoding="utf-8")
            for p in (Path(__file__).resolve().parents[1] / "routes" / "agent" / "run.py",
                      Path(__file__).resolve().parents[1] / "routes" / "agent" / "stream.py"))

    def test_predicate_imported(self):
        self.assertIn("parallel_spawn_eligible,", self.src)

    def test_both_fan_out_loops_consult_it(self):
        self.assertEqual(self.src.count("parallel_spawn_eligible(name, bool(req.plan or plan_gate)"), 2)


class ParallelReadWiring(unittest.TestCase):
    """The parallel fast path widened (2026-10-06) from an all-spawn_agent
    fan-out to also cover all-read steps. The set must be an allow-list so a
    new write/code/permission tool can never slip into concurrent execution."""

    @classmethod
    def setUpClass(cls):
        from routes.agent.constants import PARALLEL_READ_TOOLS
        cls.safe = PARALLEL_READ_TOOLS

    def test_read_tools_are_included(self):
        for t in ("read_file", "grep", "list_files", "web_fetch", "search_memory"):
            self.assertIn(t, self.safe, t)

    def test_writers_and_code_are_excluded(self):
        for t in ("write_file", "edit_file", "append_file", "revert",
                  "run_python", "run_shell",
                  "create_plan", "update_plan_item", "finish",
                  "generate_image", "generate_video"):
            self.assertNotIn(t, self.safe, t)

    def test_routes_module_uses_the_allow_list(self):
        src = "\n".join(
            p.read_text(encoding="utf-8")
            for p in (Path(__file__).resolve().parents[1] / "routes" / "agent" / "run.py",
                      Path(__file__).resolve().parents[1] / "routes" / "agent" / "stream.py"))
        self.assertIn("all_reads = bool(parsed_actions) and all(a[0] in PARALLEL_READ_TOOLS", src)


if __name__ == "__main__":
    unittest.main()
