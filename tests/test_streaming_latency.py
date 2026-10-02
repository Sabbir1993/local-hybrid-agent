"""tests/test_streaming_latency.py — Phase 2: Inference Latency & Streaming Speed.

Tests for:
  1. core/streaming_profile.py: ubatch advisor, batch_size advisor, cache_reuse
     advisor, MTP advisor, full_latency_profile aggregator.
  2. core/context_budget.py extensions: record_tps(), tps_snapshot(),
     reset_tps(), budget_latency_advice(), snapshot() TPS inclusion.

All tests are pure-python, no subprocess, no GPU, no llama-server.
Run: python -m unittest tests.test_streaming_latency -v
"""

import unittest

import core.context_budget as cb
from core.streaming_profile import (
    A770_MAX_BATCH,
    A770_MIN_BATCH,
    A770_OPTIMAL_UBATCH,
    BASELINE_TG_TPS,
    CACHE_REUSE_GRAIN,
    MTP_DRAFT_N_MAX_OPTIMAL,
    TBT_TARGET_MS,
    TTFT_TARGET_MS,
    estimate_stable_prefix,
    full_latency_profile,
    recommend_batch_size,
    recommend_cache_reuse,
    recommend_mtp,
    recommend_ubatch,
)


# ---------------------------------------------------------------------------
# 1. recommend_ubatch
# ---------------------------------------------------------------------------
class UbatchAdvisorTests(unittest.TestCase):

    def test_optimal_ubatch_unchanged(self):
        """No change when already at the optimal value."""
        r = recommend_ubatch(A770_OPTIMAL_UBATCH)
        self.assertFalse(r["changed"])
        self.assertEqual(r["ubatch_size"], A770_OPTIMAL_UBATCH)
        self.assertEqual(r["expected_gain_pct"], 0)

    def test_too_small_ubatch_recommends_increase(self):
        """ubatch=512 (default) should be flagged as too small."""
        r = recommend_ubatch(512)
        self.assertTrue(r["changed"])
        self.assertEqual(r["ubatch_size"], A770_OPTIMAL_UBATCH)
        self.assertGreater(r["expected_gain_pct"], 0)
        self.assertIn("too small", r["reason"])

    def test_too_large_ubatch_recommends_decrease(self):
        """ubatch=2048 should be flagged as too large."""
        r = recommend_ubatch(2048)
        self.assertTrue(r["changed"])
        self.assertEqual(r["ubatch_size"], A770_OPTIMAL_UBATCH)
        self.assertGreater(r["expected_gain_pct"], 0)
        self.assertIn("too large", r["reason"])

    def test_return_has_required_keys(self):
        r = recommend_ubatch(512)
        for key in ("ubatch_size", "changed", "reason", "expected_gain_pct"):
            self.assertIn(key, r, f"Missing key: {key}")


# ---------------------------------------------------------------------------
# 2. recommend_batch_size
# ---------------------------------------------------------------------------
class BatchSizeAdvisorTests(unittest.TestCase):

    def test_small_prompt_gets_min_batch(self):
        """A very short prompt gets A770_MIN_BATCH."""
        r = recommend_batch_size(100)
        self.assertEqual(r["batch_size"], A770_MIN_BATCH)

    def test_batch_size_covers_prompt(self):
        """batch_size must always be >= prompt_tokens."""
        for n_prompt in (512, 768, 1024, 1500, 2000, 3000):
            r = recommend_batch_size(n_prompt)
            self.assertGreaterEqual(r["batch_size"], n_prompt,
                                    f"batch_size {r['batch_size']} < prompt {n_prompt}")

    def test_batch_size_is_power_of_two(self):
        """Returned batch_size must be a power of 2."""
        for n_prompt in (300, 600, 1000, 2000, 4000):
            r = recommend_batch_size(n_prompt)
            bs = r["batch_size"]
            self.assertTrue(bs & (bs - 1) == 0 or bs == A770_MAX_BATCH,
                            f"batch_size {bs} is not a power of 2")

    def test_batch_size_capped_at_max(self):
        """Very long prompts get capped at A770_MAX_BATCH."""
        r = recommend_batch_size(99999)
        self.assertEqual(r["batch_size"], A770_MAX_BATCH)

    def test_return_has_required_keys(self):
        r = recommend_batch_size(1024)
        for key in ("batch_size", "prompt_tokens", "reason"):
            self.assertIn(key, r, f"Missing key: {key}")


# ---------------------------------------------------------------------------
# 3. recommend_cache_reuse
# ---------------------------------------------------------------------------
class CacheReuseAdvisorTests(unittest.TestCase):

    def test_no_stable_prefix(self):
        """Zero-length prefix → no change."""
        r = recommend_cache_reuse(stable_prefix_tokens=0, current_cache_reuse=256)
        self.assertFalse(r["changed"])

    def test_short_prefix_no_change(self):
        """A prefix smaller than current cache_reuse → no change."""
        r = recommend_cache_reuse(stable_prefix_tokens=100, current_cache_reuse=256)
        # 100 // 256 * 256 = 0 → aligned = 256 = current → no change
        self.assertFalse(r["changed"])

    def test_long_prefix_triggers_increase(self):
        """A 1500-token prefix should increase cache_reuse above 256."""
        r = recommend_cache_reuse(stable_prefix_tokens=1500, current_cache_reuse=256)
        self.assertTrue(r["changed"])
        self.assertGreater(r["cache_reuse"], 256)
        # Must be a multiple of CACHE_REUSE_GRAIN
        self.assertEqual(r["cache_reuse"] % CACHE_REUSE_GRAIN, 0)

    def test_recommended_value_covers_prefix(self):
        """Recommended cache_reuse must be ≤ stable_prefix_tokens."""
        r = recommend_cache_reuse(stable_prefix_tokens=1500, current_cache_reuse=256)
        self.assertLessEqual(r["cache_reuse"], 1500)

    def test_ttft_saving_is_positive(self):
        """TTFT saving must be positive when a change is recommended."""
        r = recommend_cache_reuse(stable_prefix_tokens=1500, current_cache_reuse=256)
        if r["changed"]:
            self.assertGreater(r["ttft_saving_ms"], 0)

    def test_already_aligned_no_change(self):
        """If cache_reuse is already aligned to the prefix, no change."""
        r = recommend_cache_reuse(stable_prefix_tokens=1024, current_cache_reuse=1024)
        self.assertFalse(r["changed"])

    def test_grain_alignment(self):
        """Every recommended cache_reuse is a multiple of CACHE_REUSE_GRAIN."""
        for prefix in (300, 700, 1100, 1500, 2000):
            r = recommend_cache_reuse(stable_prefix_tokens=prefix,
                                      current_cache_reuse=256)
            cr = r["cache_reuse"]
            self.assertEqual(cr % CACHE_REUSE_GRAIN, 0,
                             f"cache_reuse {cr} not aligned for prefix {prefix}")


# ---------------------------------------------------------------------------
# 4. recommend_mtp
# ---------------------------------------------------------------------------
class MTPAdvisorTests(unittest.TestCase):

    def test_coding_disabled_reports_potential_gain(self):
        """Disabled MTP on coding tasks reports potential_gain_pct > 30."""
        r = recommend_mtp(enabled=False, task_type="coding")
        self.assertFalse(r["enabled"])
        self.assertGreater(r.get("potential_gain_pct", 0), 30)

    def test_coding_optimal_n(self):
        """Coding tasks should recommend draft_n_max = 3."""
        r = recommend_mtp(enabled=True, current_draft_n_max=2, task_type="coding")
        self.assertEqual(r["mtp_draft_n_max"], MTP_DRAFT_N_MAX_OPTIMAL)

    def test_chat_optimal_n(self):
        """Chat tasks should recommend draft_n_max = 2 (lower accept rate)."""
        r = recommend_mtp(enabled=True, current_draft_n_max=3, task_type="chat")
        self.assertEqual(r["mtp_draft_n_max"], 2)

    def test_already_optimal_no_change(self):
        """No change when draft_n_max is already optimal for the task type."""
        r = recommend_mtp(enabled=True, current_draft_n_max=3, task_type="coding")
        self.assertFalse(r["changed"])

    def test_throughput_gain_positive(self):
        """expected_gain_pct must be positive for enabled MTP."""
        r = recommend_mtp(enabled=True, current_draft_n_max=3, task_type="coding")
        self.assertGreater(r.get("expected_gain_pct", 0), 0)

    def test_unknown_task_type_uses_generic(self):
        """An unknown task type falls back to generic parameters."""
        r = recommend_mtp(enabled=True, current_draft_n_max=1, task_type="unknown_xyz")
        self.assertIn(r["mtp_draft_n_max"], (2, 3))

    def test_all_return_fields_present(self):
        for task in ("coding", "chat", "math", "generic"):
            r = recommend_mtp(enabled=True, task_type=task)
            for key in ("mtp_draft_n_max", "enabled", "changed", "task_type", "reason"):
                self.assertIn(key, r, f"Missing key '{key}' for task '{task}'")


# ---------------------------------------------------------------------------
# 5. estimate_stable_prefix
# ---------------------------------------------------------------------------
class StablePrefixTests(unittest.TestCase):

    def test_empty_inputs_return_zero(self):
        self.assertEqual(estimate_stable_prefix("", ""), 0)

    def test_nonempty_system_prompt(self):
        """A 500-char system prompt should estimate > 100 tokens."""
        sp = "x" * 500
        tok = estimate_stable_prefix(sp, "")
        self.assertGreater(tok, 100)

    def test_longer_tools_json_more_tokens(self):
        """Adding a long tools_json increases the estimate."""
        t1 = estimate_stable_prefix("system", "{}")
        t2 = estimate_stable_prefix("system", "{}" * 500)
        self.assertGreater(t2, t1)

    def test_result_is_integer(self):
        r = estimate_stable_prefix("sys", '{"tools": []}')
        self.assertIsInstance(r, int)


# ---------------------------------------------------------------------------
# 6. full_latency_profile
# ---------------------------------------------------------------------------
class FullLatencyProfileTests(unittest.TestCase):

    def _make_profile(self, **kwargs):
        base = {
            "ubatch_size": 512,
            "batch_size": 2048,
            "cache_reuse": 256,
            "mtp_enabled": False,
            "mtp_draft_n_max": 3,
            "flash_attn": "auto",
        }
        base.update(kwargs)
        return base

    def test_returns_required_keys(self):
        r = full_latency_profile(self._make_profile())
        for key in ("changes", "summary", "estimated_tps", "baseline_tps",
                    "stable_prefix_tokens"):
            self.assertIn(key, r, f"Missing key: {key}")

    def test_changes_is_list(self):
        r = full_latency_profile(self._make_profile())
        self.assertIsInstance(r["changes"], list)

    def test_non_optimal_ubatch_generates_change(self):
        """Profile with ubatch=512 should recommend a ubatch change."""
        r = full_latency_profile(self._make_profile(ubatch_size=512))
        params = [c["param"] for c in r["changes"]]
        self.assertIn("ubatch_size", params)

    def test_optimal_ubatch_no_ubatch_change(self):
        """Profile with ubatch=1024 should NOT recommend an ubatch change."""
        r = full_latency_profile(self._make_profile(ubatch_size=A770_OPTIMAL_UBATCH))
        params = [c["param"] for c in r["changes"]]
        self.assertNotIn("ubatch_size", params)

    def test_estimated_tps_above_zero(self):
        r = full_latency_profile(self._make_profile())
        self.assertGreater(r["estimated_tps"], 0)

    def test_estimated_tps_with_mtp_enabled_higher(self):
        """Enabling MTP should raise the estimated TPS."""
        r_off = full_latency_profile(self._make_profile(mtp_enabled=False))
        r_on  = full_latency_profile(self._make_profile(mtp_enabled=True))
        self.assertGreater(r_on["estimated_tps"], r_off["estimated_tps"])

    def test_long_stable_prefix_triggers_cache_reuse_change(self):
        """A long system prompt + tools_json should recommend a cache_reuse increase."""
        sp = "You are a coding assistant. " * 50   # ~1400 chars
        tj = '{"name": "tool", "description": "desc"} ' * 40  # ~2000 chars
        r = full_latency_profile(self._make_profile(), system_prompt=sp,
                                 tools_json=tj)
        params = [c["param"] for c in r["changes"]]
        self.assertIn("cache_reuse", params,
                      f"Expected cache_reuse change, got params: {params}")

    def test_change_entries_have_required_keys(self):
        r = full_latency_profile(self._make_profile())
        for change in r["changes"]:
            for key in ("param", "to", "reason"):
                self.assertIn(key, change, f"Change missing key '{key}': {change}")


# ---------------------------------------------------------------------------
# 7. TPS EMA tracking in context_budget
# ---------------------------------------------------------------------------
class TPSTrackingTests(unittest.TestCase):

    def setUp(self):
        cb.reset_tps()   # clean slate

    def tearDown(self):
        cb.reset_tps()

    def test_cold_start_ema_seeded_from_first_sample(self):
        """First record_tps call seeds the EMA directly."""
        cb.record_tps("main", tokens_generated=100, elapsed_s=5.0)  # 20 tok/s
        snap = cb.tps_snapshot()
        self.assertAlmostEqual(snap["main"]["ema_tps"], 20.0, places=1)

    def test_ema_converges_toward_new_rate(self):
        """EMA should converge toward the new rate after many samples."""
        for _ in range(30):
            cb.record_tps("main", tokens_generated=220, elapsed_s=10.0)  # 22 tok/s
        snap = cb.tps_snapshot()
        self.assertAlmostEqual(snap["main"]["ema_tps"], 22.0, places=0)

    def test_invalid_inputs_ignored(self):
        """Zero or negative elapsed/tokens must not crash or record anything."""
        cb.record_tps("main", tokens_generated=0, elapsed_s=5.0)
        cb.record_tps("main", tokens_generated=100, elapsed_s=0.0)
        cb.record_tps("main", tokens_generated=-5, elapsed_s=2.0)
        snap = cb.tps_snapshot()
        self.assertNotIn("main", snap)

    def test_multiple_lanes_independent(self):
        """Different lanes must not bleed into each other."""
        cb.record_tps("main", 100, 5.0)      # 20 tok/s
        cb.record_tps("executor", 300, 5.0)  # 60 tok/s
        snap = cb.tps_snapshot()
        self.assertAlmostEqual(snap["main"]["ema_tps"], 20.0, places=1)
        self.assertAlmostEqual(snap["executor"]["ema_tps"], 60.0, places=1)

    def test_n_samples_increments(self):
        for i in range(5):
            cb.record_tps("main", 100, 5.0)
        snap = cb.tps_snapshot()
        self.assertEqual(snap["main"]["n_samples"], 5)

    def test_reset_clears_lane(self):
        cb.record_tps("main", 100, 5.0)
        cb.reset_tps("main")
        snap = cb.tps_snapshot()
        self.assertNotIn("main", snap)

    def test_reset_all_clears_everything(self):
        cb.record_tps("main", 100, 5.0)
        cb.record_tps("executor", 100, 5.0)
        cb.reset_tps()
        self.assertEqual(cb.tps_snapshot(), {})

    def test_snapshot_includes_tps_in_main_snapshot(self):
        """snapshot() should include ema_tps when TPS has been recorded."""
        cb.reset(lane="main")
        cb.window("main", cfg_ctx=32768)
        cb.record_tps("main", 100, 5.0)
        snap = cb.snapshot()
        # main lane exists in snapshot and has ema_tps
        self.assertIn("main", snap)
        self.assertIn("ema_tps", snap["main"])
        self.assertAlmostEqual(snap["main"]["ema_tps"], 20.0, places=1)


# ---------------------------------------------------------------------------
# 8. budget_latency_advice
# ---------------------------------------------------------------------------
class LatencyAdviceTests(unittest.TestCase):

    def setUp(self):
        cb.reset_tps()

    def tearDown(self):
        cb.reset_tps()

    def test_cold_start_no_below_target(self):
        """Without any TPS samples, below_target must be False (no data)."""
        r = cb.budget_latency_advice("main")
        self.assertFalse(r["below_target"])
        self.assertIsNone(r["ema_tps"])

    def test_fast_lane_not_below_target(self):
        """A lane running at 30 tok/s (above target) must not flag below_target."""
        for _ in range(5):
            cb.record_tps("main", 300, 10.0)  # 30 tok/s
        r = cb.budget_latency_advice("main")
        self.assertFalse(r["below_target"])
        self.assertGreater(r["ema_tps"], 20.0)

    def test_slow_lane_flags_below_target(self):
        """A lane running at 10 tok/s (below target) must flag below_target=True."""
        for _ in range(5):
            cb.record_tps("main", 100, 10.0)  # 10 tok/s
        r = cb.budget_latency_advice("main")
        self.assertTrue(r["below_target"])

    def test_advice_dict_present(self):
        """Advice dict must always be present (even with no profile)."""
        r = cb.budget_latency_advice("main", profile={})
        self.assertIn("advice", r)
        self.assertIsInstance(r["advice"], dict)

    def test_required_keys_present(self):
        r = cb.budget_latency_advice("executor")
        for key in ("lane", "ema_tps", "target_tps", "below_target", "advice"):
            self.assertIn(key, r, f"Missing key: {key}")

    def test_target_tps_matches_module_constant(self):
        r = cb.budget_latency_advice("main")
        self.assertEqual(r["target_tps"], cb._TBT_TARGET_TPS)


# ---------------------------------------------------------------------------
# 9. Hardware constant sanity checks
# ---------------------------------------------------------------------------
class HardwareConstantTests(unittest.TestCase):

    def test_optimal_ubatch_in_valid_range(self):
        self.assertGreaterEqual(A770_OPTIMAL_UBATCH, A770_MIN_BATCH)
        self.assertLessEqual(A770_OPTIMAL_UBATCH, A770_MAX_BATCH)

    def test_cache_reuse_grain_is_power_of_two(self):
        self.assertTrue(CACHE_REUSE_GRAIN & (CACHE_REUSE_GRAIN - 1) == 0)

    def test_mtp_draft_n_max_in_valid_range(self):
        # Must satisfy CONFIG_INT_FIELDS mtp_draft_n_max: (1, 16)
        self.assertGreaterEqual(MTP_DRAFT_N_MAX_OPTIMAL, 1)
        self.assertLessEqual(MTP_DRAFT_N_MAX_OPTIMAL, 16)

    def test_baseline_tg_tps_above_zero(self):
        self.assertGreater(BASELINE_TG_TPS, 0)

    def test_ttft_target_reasonable(self):
        # UI target: 200ms – 2000ms
        self.assertGreater(TTFT_TARGET_MS, 200)
        self.assertLess(TTFT_TARGET_MS, 2000)

    def test_tbt_target_reasonable(self):
        # 10ms = 100 tok/s (unrealistic), 200ms = 5 tok/s (barely usable)
        self.assertGreater(TBT_TARGET_MS, 10)
        self.assertLess(TBT_TARGET_MS, 200)


if __name__ == "__main__":
    unittest.main()
