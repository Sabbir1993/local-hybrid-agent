import unittest

from core import lane_health as lh
from core import step_outcome as so


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class TurnOkTests(unittest.TestCase):
    def test_step0_no_tool_call_fails_only_action_requests(self):
        self.assertFalse(lh.turn_ok(so.NO_TOOL_CALL, "action", 0, False))
        self.assertTrue(lh.turn_ok(so.NO_TOOL_CALL, "question", 0, False))
        self.assertTrue(lh.turn_ok(so.NO_TOOL_CALL, "action", 4, False))   # a later final answer
        self.assertFalse(lh.turn_ok(so.EMPTY, "creation", 0, False))

    def test_escalation_and_hard_failures_always_fail(self):
        self.assertFalse(lh.turn_ok(so.TOOL_CALL, "action", 0, True))
        for o in (so.TRANSPORT_ERROR, so.PARSE_FAIL, so.LOOP):
            self.assertFalse(lh.turn_ok(o, "question", 3, False))
        self.assertTrue(lh.turn_ok(so.TOOL_CALL, "action", 0, False))


class RoutingDecisionTests(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self.h = lh.LaneHealth(clock=self.clock)

    def _fill(self, ok_flags, lane="executor", cat="action"):
        for ok in ok_flags:
            self.h.record(lane, cat, ok)

    def test_too_few_samples_never_demotes(self):
        self._fill([False] * 3, cat="action")     # 3 < BREAKER_FAILURES, < MIN_SAMPLES
        self.assertEqual(self.h.prefer_main("executor", "action"), "")
        self.assertIsNone(self.h.success_rate("executor", "action"))

    def test_low_success_rate_sends_the_category_to_main(self):
        self._fill([True, False] * 5)             # 10 turns, 50%, no long failure streak
        self.assertEqual(self.h.prefer_main("executor", "action"), "adaptive_low_success")
        self.assertEqual(self.h.prefer_main("executor", "question"), "")   # other categories unaffected

    def test_healthy_lane_is_left_alone(self):
        self._fill([True] * 9 + [False])
        self.assertEqual(self.h.prefer_main("executor", "action"), "")

    def test_breaker_opens_after_consecutive_failures_across_categories(self):
        self._fill([False] * lh.BREAKER_FAILURES, cat="question")
        self.assertTrue(self.h.breaker_open("executor"))
        self.assertEqual(self.h.prefer_main("executor", "action"), "breaker_open")

    def test_demoted_lane_is_probed_after_cooldown_and_recovers(self):
        self._fill([False] * lh.BREAKER_FAILURES)
        self.assertEqual(self.h.prefer_main("executor", "action"), "breaker_open")
        self.clock.t += lh.COOLDOWN_S + 1
        self.assertEqual(self.h.prefer_main("executor", "action"), "")             # one probe goes through
        self.assertEqual(self.h.prefer_main("executor", "action"), "breaker_open")  # then waits again
        self.h.record("executor", "action", True)                                   # probe succeeded
        self.assertFalse(self.h.breaker_open("executor"))
        self.assertEqual(self.h.prefer_main("executor", "action"), "")

    def test_low_rate_recovers_through_probes(self):
        self._fill([False, True] * 5)
        self.assertEqual(self.h.prefer_main("executor", "action"), "adaptive_low_success")
        for _ in range(8):                        # probes keep succeeding after each cooldown
            self.clock.t += lh.COOLDOWN_S + 1
            self.assertEqual(self.h.prefer_main("executor", "action"), "")
            self.h.record("executor", "action", True)
        self.assertEqual(self.h.prefer_main("executor", "action"), "")

    def test_snapshot_reports_state(self):
        self._fill([True, False])
        snap = self.h.snapshot()
        self.assertEqual(snap["executor"]["categories"]["action"]["turns"], 2)
        self.assertFalse(snap["executor"]["breaker_open"])

    def test_hydrate_replays_history_oldest_first(self):
        rows = [("executor", "action", so.NO_TOOL_CALL, 0, 0)] * lh.BREAKER_FAILURES
        self.h.hydrate(rows)
        self.assertTrue(self.h.breaker_open("executor"))


class PolicyWiringTests(unittest.TestCase):
    def test_configured_category_wins_and_kill_switch_disables_adaptive(self):
        from core import router_policy as rp
        cfg = dict(rp.DEFAULTS, start_on_main_categories=["action"])
        self.assertEqual(rp.main_first_reason("action", cfg), "start_on_main")
        off = dict(rp.DEFAULTS, adaptive=False)
        self.assertEqual(rp.main_first_reason("action", off), "")


if __name__ == "__main__":
    unittest.main()
