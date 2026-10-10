"""tests/test_run_budget.py - the run token budget counts new tokens, steers at 50/70/90% and allows a short grace.

Run: python -m unittest tests.test_run_budget -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.agent_loop import budget as B


class AccountingTests(unittest.TestCase):
    def test_first_call_counts_the_whole_prompt(self):
        b = B.RunBudget(1000)
        self.assertEqual(b.add(300, 50), 350)

    def test_growing_history_counts_only_what_is_new(self):
        b = B.RunBudget(10000)
        b.add(1000, 100)                               # whole prompt + reply
        added = b.add(1400, 80)                        # prompt grew by 400, of which 100 was the previous reply
        self.assertEqual(added, (1400 - 1000 - 100) + 80)
        self.assertEqual(b.used, 1100 + 380)

    def test_a_rewritten_prefix_costs_the_full_prompt(self):
        b = B.RunBudget(10000)
        b.add(1000, 100)
        self.assertEqual(b.add(700, 50, prefix_rewritten=True), 750)

    def test_provider_cached_tokens_are_not_counted(self):
        b = B.RunBudget(10000)
        b.add(1000, 100)
        self.assertEqual(b.add(1400, 80, cached_tokens=1200), (1400 - 1200) + 80)

    def test_105_steps_of_a_24k_history_stay_far_below_the_old_total(self):
        b = B.RunBudget(2_500_000)
        for i in range(105):
            b.add(24000 + i * 300, 150)               # the old counting would have summed ~2.5M here
        self.assertLess(b.used, 500_000)


class SteeringTests(unittest.TestCase):
    def test_stages(self):
        b = B.RunBudget(1000)
        self.assertEqual(b.stage(), B.STAGE_OK)
        b.used = 500
        self.assertEqual(b.stage(), B.STAGE_HALF)
        b.used = 700
        self.assertEqual(b.stage(), B.STAGE_SQUEEZE)
        b.used = 900
        self.assertEqual(b.stage(), B.STAGE_FINISH)

    def test_grace_lets_the_item_in_progress_finish_then_stops(self):
        b = B.RunBudget(1000)
        b.used = 1000
        self.assertFalse(b.exhausted())                # at the limit: grace, not a stop
        for _ in range(B.GRACE_STEPS):
            b.note_step()
        self.assertTrue(b.exhausted())                 # grace steps used up

    def test_grace_is_capped_by_tokens_too(self):
        b = B.RunBudget(1000)
        b.used = 1101
        self.assertTrue(b.exhausted())

    def test_disabled_budget_never_stops(self):
        b = B.RunBudget(0)
        b.used = 10**9
        self.assertFalse(b.enabled)
        self.assertFalse(b.exhausted())
        self.assertEqual(b.stage(), B.STAGE_OK)


if __name__ == "__main__":
    unittest.main()
