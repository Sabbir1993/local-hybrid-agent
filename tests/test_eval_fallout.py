"""tests/test_eval_fallout.py - R2: fallout@k gating direction.

Fallout is the counterweight to recall: a tuning step that "wins" recall by
loosening gates must fail the regression gate on rising fallout. These tests
pin the comparator's direction on synthetic dicts (no slice run needed - the
slice itself is exercised by every mock invocation).

Run: python -m unittest tests.test_eval_fallout -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from eval_mock import _summarize_suite, compare_baseline

FALLOUT_KEYS = ("fallout_at_k", "lexical_fallout_at_k", "paraphrase_fallout_at_k",
                "lexonly_fallout_at_k",
                "hybrid_real_fallout_at_k",
                "hybrid_real_lexical_fallout_at_k",
                "hybrid_real_paraphrase_fallout_at_k")


def _summ(retrieval=None, **kw):
    base = {"tasks": {}, "aggregate_rate": 1.0, "retrieval": retrieval,
            "router": None, "offline": None}
    base.update(kw)
    return base


class FalloutDirectionTests(unittest.TestCase):
    def test_fallout_rise_is_a_regression(self):
        for key in FALLOUT_KEYS:
            with self.subTest(key=key):
                problems = compare_baseline(
                    _summ(retrieval={key: 0.5}),
                    _summ(retrieval={key: 0.2}))
                self.assertTrue(any(key in p for p in problems),
                                f"{key} rise 0.2 -> 0.5 not flagged: {problems}")

    def test_fallout_drop_is_silent(self):
        for key in FALLOUT_KEYS:
            with self.subTest(key=key):
                problems = compare_baseline(
                    _summ(retrieval={key: 0.1}),
                    _summ(retrieval={key: 0.4}))
                self.assertFalse(any(key in p for p in problems),
                                 f"{key} drop 0.4 -> 0.1 wrongly flagged: {problems}")

    def test_fallout_at_tolerance_is_silent(self):
        problems = compare_baseline(
            _summ(retrieval={"lexonly_fallout_at_k": 0.201}),
            _summ(retrieval={"lexonly_fallout_at_k": 0.2}))
        self.assertFalse(any("fallout" in p for p in problems), problems)

    def test_real_keys_tolerate_embed_wobble(self):
        # R5 measured ~0.005 aggregate drift re-embedding identical texts with
        # a fresh server process: below 0.01 stays silent, real drops still fire.
        problems = compare_baseline(
            _summ(retrieval={"hybrid_real_fallout_at_k": 0.412}),
            _summ(retrieval={"hybrid_real_fallout_at_k": 0.407}))
        self.assertFalse(any("fallout" in p for p in problems), problems)
        problems = compare_baseline(
            _summ(retrieval={"hybrid_real_recall_at_k": 0.939}),
            _summ(retrieval={"hybrid_real_recall_at_k": 0.944}))
        self.assertFalse(any("recall" in p for p in problems), problems)
        problems = compare_baseline(
            _summ(retrieval={"hybrid_real_fallout_at_k": 0.5}),
            _summ(retrieval={"hybrid_real_fallout_at_k": 0.407}))
        self.assertTrue(any("hybrid_real_fallout_at_k" in p for p in problems),
                        problems)

    def test_recall_drop_still_flagged(self):
        problems = compare_baseline(
            _summ(retrieval={"lexonly_recall_at_k": 0.5}),
            _summ(retrieval={"lexonly_recall_at_k": 1.0}))
        self.assertTrue(any("lexonly_recall_at_k" in p for p in problems), problems)

    def test_unmeasured_keys_are_silent_either_side(self):
        # New measurement, no history (or history, no measurement this run):
        # informational, never a regression - same rule as new tasks.
        self.assertEqual(compare_baseline(
            _summ(retrieval={"lexonly_fallout_at_k": 0.9}),
            _summ(retrieval={})), [])
        self.assertEqual(compare_baseline(
            _summ(retrieval={}),
            _summ(retrieval={"lexonly_fallout_at_k": 0.1})), [])
        self.assertEqual(compare_baseline(
            _summ(retrieval=None),
            _summ(retrieval={"lexonly_fallout_at_k": 0.1})), [])


class SummarizeFalloutTests(unittest.TestCase):
    def test_summarize_carries_fallout_and_drops_nones(self):
        suite = {"tasks": {}, "aggregate": {"pass_rate": 1.0},
                 "retrieval": {"lexonly_recall_at_k": 1.0,
                               "lexonly_fallout_at_k": 0.42,
                               "fallout_at_k": None,
                               "hybrid_real_fallout_at_k": 0.1},
                 "router": None}
        out = _summarize_suite(suite, None)
        self.assertEqual(out["retrieval"]["lexonly_fallout_at_k"], 0.42)
        self.assertEqual(out["retrieval"]["hybrid_real_fallout_at_k"], 0.1)
        self.assertNotIn("fallout_at_k", out["retrieval"])


class ScoringDefaultsTests(unittest.TestCase):
    """R4: the calibrated scoring defaults are pinned, not drifted on.

    min_cos=0.55 is the grid knee (real fallout 0.771 -> 0.407 for one hard
    paraphrase miss; 0.65 vetoed - paraphrase collapses to 0.6). min_lex=2 is
    the lexonly knee (1.0/0.369). Weights stay 0.5/0.5 for LACK of evidence
    (0/48 grid cells varied with them in either mode). Any change here must
    come with a new sweep table, not a hunch.
    """

    def test_r4_winners_are_the_defaults(self):
        import inspect
        from core.memory import search as search_mod
        params = inspect.signature(search_mod.search_knowledge_hybrid).parameters
        self.assertEqual(params["min_cos"].default, 0.55)
        self.assertEqual(params["min_lex"].default, 2)
        self.assertEqual(params["cos_weight"].default, 0.5)
        self.assertEqual(params["lex_weight"].default, 0.5)
        self.assertEqual(params["title_boost_match"].default, 0.35)
        self.assertEqual(params["title_boost_broad"].default, 0.20)
        self.assertEqual(params["use_title_lex"].default, False)
        # R6 winners: rerank on, stage-1 0.45, floor on (recovers the R4
        # paraphrase casualty at rank 2: R 0.944->1.0 for F 0.412->0.44).
        self.assertEqual(params["rerank"].default, True)
        self.assertEqual(params["rerank_candidates"].default, 20)
        self.assertEqual(params["rerank_min_cos"].default, 0.45)
        self.assertEqual(params["rerank_floor"].default, True)


if __name__ == "__main__":
    unittest.main()
