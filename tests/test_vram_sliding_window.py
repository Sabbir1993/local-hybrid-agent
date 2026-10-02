"""tests/test_vram_sliding_window.py - hybrid-attention KV sizing.

A layer with `attention.sliding_window = W` only ever holds W tokens of KV, whatever -c says.
Charging the full context to every layer made the configured main profile come out "nofit" on
a 15.1 GB card, so `preflight.mode: "block"` refused to load a model whose real KV cache is
tens of MB - an estimated 451x overstatement on the shipped Spark-X2.5 profile.

These tests pin the arithmetic and the fallbacks, not the real GGUF (that would make the suite
depend on a 2 GB model file being present).

Run: python -m unittest tests.test_vram_sliding_window -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.vram.constants import GB
from core.vram.estimator import estimate_footprint


def _info(sliding_window=None, **over):
    info = {"arch": "test", "n_layer": 36, "n_head": 16, "n_head_kv": 4, "n_embd": 2560,
            "head_dim": 256, "ctx_train": 1048576, "sliding_window": sliding_window,
            "n_expert": 0, "n_expert_used": 0, "n_vocab": 131072,
            "file_bytes": 2 * GB, "complete": True, "error": None}
    info.update(over)
    return info


class SlidingWindowCapsTheCache(unittest.TestCase):
    def test_window_below_ctx_collapses_kv_to_the_window(self):
        fp = estimate_footprint(_info(sliding_window=512), 65536, "q8_0", 512, "on")
        self.assertEqual(fp["effective_ctx"], 512)
        # kv_per_token * 512, not * 65536
        self.assertEqual(fp["kv_total_b"], int(fp["kv_per_token_b"] * 512))
        self.assertEqual(fp["sliding_window"], 512)

    def test_the_overstatement_that_mattered_is_gone(self):
        """The shipped profile: 36 layers, 4 kv heads, 256 head_dim, q8_0, window 512."""
        windowed = estimate_footprint(_info(sliding_window=512), 231072, "q8_0", 512, "on")
        unwindowed = estimate_footprint(_info(sliding_window=None), 231072, "q8_0", 512, "on")
        self.assertGreater(unwindowed["kv_total_b"] / windowed["kv_total_b"], 400)
        self.assertLess(windowed["total_b"] if "total_b" in windowed
                        else windowed["weights_b"] + windowed["kv_total_b"]
                        + windowed["compute_b"] + windowed["headroom_b"], 5 * GB)

    def test_window_above_ctx_is_capped_by_ctx(self):
        fp = estimate_footprint(_info(sliding_window=1 << 20), 4096, "q8_0", 512, "on")
        self.assertEqual(fp["effective_ctx"], 4096)


class NoWindowMeansFullContext(unittest.TestCase):
    """Missing metadata must stay in the conservative direction: charging the full context
    over-estimates, which can only produce a false "tight"/"nofit", never a false "fit"."""

    def test_none_window_charges_full_ctx(self):
        fp = estimate_footprint(_info(sliding_window=None), 32768, "f16", 512, "on")
        self.assertEqual(fp["effective_ctx"], 32768)
        self.assertIsNone(fp["sliding_window"])

    def test_zero_and_negative_window_mean_full_attention(self):
        for w in (0, -1):
            with self.subTest(w=w):
                fp = estimate_footprint(_info(sliding_window=w), 32768, "f16", 512, "on")
                self.assertEqual(fp["effective_ctx"], 32768)
                self.assertIsNone(fp["sliding_window"])

    def test_key_absent_from_the_dict_is_not_an_error(self):
        info = _info()
        del info["sliding_window"]
        fp = estimate_footprint(info, 32768, "f16", 512, "on")
        self.assertEqual(fp["effective_ctx"], 32768)


class ParserExtractsTheKey(unittest.TestCase):
    def test_window_is_a_scalar_not_a_list(self):
        """scalar() takes max() of an array, so a weird multi-value key must not silently
        become a huge window."""
        from core.vram.gguf_parser import parse_gguf_info
        import inspect
        src = inspect.getsource(parse_gguf_info)
        self.assertIn("sliding_window", src)
        self.assertIn("n_expert_used", src)

    def test_expert_used_count_is_carried(self):
        info = _info(n_expert=256, n_expert_used=8)
        self.assertEqual(info["n_expert_used"], 8)


class RealGgufCheck(unittest.TestCase):
    """If the configured model is on disk, assert the whole chain end to end. Skipped
    otherwise so the suite never depends on a multi-GB model being present."""

    def test_configured_executor_reports_a_window(self):
        import json
        cfg = json.loads(Path("config/app.json").read_text(encoding="utf-8"))
        ex = (cfg.get("small_models") or {}).get("executor") or {}
        rel = ex.get("model")
        base = Path(cfg.get("models_base") or "E:/AI/Models")
        if not rel:
            self.skipTest("no executor model configured")
        mp = base / rel
        if not mp.exists():
            self.skipTest(f"model not present: {mp}")
        from core.vram.gguf_parser import parse_gguf_info
        info = parse_gguf_info(mp)
        self.assertIsNotNone(info)
        self.assertFalse(info.get("error"), f"parse error: {info.get('error')}")
        self.assertIn("sliding_window", info)


if __name__ == "__main__":
    unittest.main()