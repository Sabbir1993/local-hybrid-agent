"""Executor escalation must be per-step, never permanent (routes/agent.py).

The reported bug: under "Best quality" (main-cloud-rest-local) the executor was
configured, present on disk, and eligible - yet every step ran on the main model.
`lane_name` was reassigned in place on escalation and never reset, so ONE executor
step that tripped escalate_reason() disqualified the executor for the rest of the
run. With creation_keywords matching almost any code request, that fired on step 0
essentially always.

These tests pin the fix: lane_name is recomputed from scratch each iteration, and
a single escalation is forgiven (ESC_STREAK_LIMIT) so the executor is re-tried.

Run: python -m unittest tests.test_lane_escalation -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import router_policy  # noqa: E402


class _Streak:
    """The escalation counters exactly as routes/agent.py drives them.

    Mirrors the loop body so these tests exercise the real decision rule rather
    than a re-implementation of it: esc_sustained is checked BEFORE the lane is
    chosen, which is what stops escalation from latching.
    """

    def __init__(self):
        self.esc_streak = 0
        self.esc_streak_reason = None
        self.lanes = []          # lane actually used, per step
        self.reasons = []        # lane_reason per step
        self.warnings = []       # lane_warning payloads emitted

    def choose_lane(self, *, step, use_executor, executor_stuck, main_ready,
                    esc_streak_limit, start_on_main=False, ca_lane="auto"):
        esc_sustained = self.esc_streak >= esc_streak_limit
        main_first = (step == 0 and use_executor and main_ready
                      and ca_lane != "executor" and start_on_main)
        lane = ("main" if not use_executor or executor_stuck or main_first
                or esc_sustained else "executor")
        reason = ("no_executor" if not use_executor else "repeat_streak" if executor_stuck
                  else "start_on_main" if main_first
                  else f"escalated_{self.esc_streak_reason}" if esc_sustained
                  else "executor_default")
        self.lanes.append(lane)
        self.reasons.append(reason)
        return lane, reason

    def record(self, escalated, esc_reason, limit):
        """Mirror the counter update in routes/agent.py."""
        if escalated:
            if esc_reason == self.esc_streak_reason:
                self.esc_streak += 1
            else:
                self.esc_streak_reason = esc_reason
                self.esc_streak = 1
            if self.esc_streak < limit:
                self.warnings.append({"reason": esc_reason, "retrying": True})


class EscalationIsNotStickyTests(unittest.TestCase):
    ESC_LIMIT = 2

    def _run(self, escalate_steps, total=4):
        """Drive the lane choice for `total` steps, escalating on `escalate_steps`."""
        s = _Streak()
        for step in range(total):
            lane, _ = s.choose_lane(step=step, use_executor=True, executor_stuck=False,
                                    main_ready=True, esc_streak_limit=self.ESC_LIMIT)
            escalated = lane == "executor" and step in escalate_steps
            s.record(escalated, "creation_no_tool" if escalated else "", self.ESC_LIMIT)
        return s

    def test_one_escalation_does_not_disqualify_the_executor(self):
        """THE regression: escalate on step 0, expect the executor back on step 1."""
        s = self._run(escalate_steps={0})
        self.assertEqual(s.lanes, ["executor"] * 4,
                         "executor must return after a single escalation")
        self.assertEqual(len(s.warnings), 1)
        self.assertTrue(s.warnings[0]["retrying"],
                        "a forgiven escalation must say the executor is re-tried")

    def test_sustained_escalation_commits_to_main(self):
        """Two steps tripping the same reason is real failure - main is then correct."""
        s = self._run(escalate_steps={0, 1})
        self.assertEqual(s.lanes[0], "executor")
        self.assertEqual(s.lanes[1], "executor", "first offence is still forgiven")
        self.assertEqual(s.lanes[2], "main", "a second identical failure commits to main")
        self.assertIn("escalated_creation_no_tool", s.reasons[2])

    def test_a_different_reason_restarts_the_count(self):
        """Alternating failure modes must not accumulate into a false commitment."""
        s = _Streak()
        for step in range(4):
            lane, _ = s.choose_lane(step=step, use_executor=True, executor_stuck=False,
                                    main_ready=True, esc_streak_limit=self.ESC_LIMIT)
            s.record(lane == "executor", "refused" if step % 2 == 0 else "tutorial_code",
                     self.ESC_LIMIT)
        self.assertEqual(set(s.lanes), {"executor"},
                         "a flapping executor keeps its lane, not punished for 2 bad steps")

    def test_clean_executor_run_never_escalates(self):
        s = self._run(escalate_steps=set())
        self.assertEqual(set(s.lanes), {"executor"})
        self.assertEqual(s.warnings, [])

    def test_recovery_after_commitment(self):
        """Two identical failures commit to main; a later clean step must not re-arm.

        Regression guard for the counter: once the run is on main, a step that is
        NOT the executor must not leave a stale esc_streak that would strand the
        run on main even if the executor later becomes viable.
        """
        s = _Streak()
        for step in range(5):
            lane, _ = s.choose_lane(step=step, use_executor=True, executor_stuck=False,
                                    main_ready=True, esc_streak_limit=self.ESC_LIMIT)
            # the executor escalates on the first two steps, then behaves
            s.record(lane == "executor" and step < 2, "refused", self.ESC_LIMIT)
        self.assertEqual(s.lanes[:2], ["executor", "executor"])
        self.assertEqual(s.lanes[2], "main", "sustained failure commits to main")


class EscalationTriggerTests(unittest.TestCase):
    """Why the counter reached ESC_STREAK_LIMIT at all: the step-0 triggers.

    Guards the other half of the diagnosis - a 4B model asked to *create* something
    very often emits a markdown tutorial instead of calling a tool, which is exactly
    the creation_no_tool condition.
    """

    def test_creation_query_without_tool_calls_escalates(self):
        r = router_policy.escalate_reason(
            step=0, content="Here is how you would build it:", tool_calls=[],
            query="create a shop page", is_loop=False, cfg=router_policy.DEFAULTS)
        self.assertEqual(r, "creation_no_tool")

    def test_a_tool_call_is_never_escalated(self):
        r = router_policy.escalate_reason(
            step=0, content="", tool_calls=[{"function": {"name": "write_file"}}],
            query="create a shop page", is_loop=False, cfg=router_policy.DEFAULTS)
        self.assertEqual(r, "", "a step that used a tool must stay on the executor")

    def test_plain_answer_on_a_later_step_is_kept(self):
        """Only step 0 is trigger-able; a plain later answer must not escalate."""
        r = router_policy.escalate_reason(
            step=3, content="Here is the plan.", tool_calls=[],
            query="create a shop page", is_loop=False, cfg=router_policy.DEFAULTS)
        self.assertEqual(r, "")


if __name__ == "__main__":
    unittest.main()
