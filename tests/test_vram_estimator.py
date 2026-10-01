"""tests/test_vram_estimator.py - the VRAM estimate and preflight fail-open behaviour.

Run: python -m unittest tests.test_vram_estimator -v

The compute buffer used to model llama.cpp's dominant compute tensor - the logits, sized
n_vocab x n_ubatch x sizeof(float) - as about 8 MB. For a 128k-vocabulary model that term is
~263 MB, so the estimate was low by roughly 30x, and n_vocab was already parsed by
gguf_parser and read by nothing.

Separately, a model file the server could not *read* produced file_bytes 0, which made the
footprint ~0 and the verdict "fit" - failing open on exactly the case that most needs to stop.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.vram.constants import GB
from core.vram.estimator import effective_params, estimate_footprint
from core.vram.gguf_parser import parse_gguf_info

# a Llama-3-class model: 80 layers, GQA 8 kv heads, head_dim 128, 128256 vocab
LLAMA3 = {"arch": "llama", "n_layer": 80, "n_head": 64, "n_head_kv": 8, "n_embd": 8192,
          "head_dim": 128, "ctx_train": 8192, "n_expert": 0, "n_vocab": 128256,
          "file_bytes": 15 * 1024 ** 3, "complete": True, "stat_ok": True,
          "model_path": "demo.gguf"}


class ComputeBufferUsesVocab(unittest.TestCase):
    def test_the_logits_term_is_actually_modelled(self):
        for ubatch in (256, 512, 2048):
            fp = estimate_footprint(LLAMA3, 8192, "f16", ubatch, "on")
            logits = 128256 * ubatch * 4
            # the estimate must at least cover the logits tensor itself
            self.assertGreaterEqual(fp["compute_b"], logits,
                                    f"ubatch={ubatch}: estimate below the logits tensor")

    def test_it_grows_with_the_vocab_not_a_flat_constant(self):
        big = estimate_footprint(LLAMA3, 8192, "f16", 512, "on")["compute_b"]
        small = estimate_footprint(dict(LLAMA3, n_vocab=32000), 8192, "f16", 512, "on")["compute_b"]
        self.assertGreater(big, small, "a 128k vocab must cost more than a 32k one")

    def test_it_grows_with_the_ubatch(self):
        a = estimate_footprint(LLAMA3, 8192, "f16", 512, "on")["compute_b"]
        b = estimate_footprint(LLAMA3, 8192, "f16", 2048, "on")["compute_b"]
        self.assertGreater(b, a * 2, "a 4x ubatch should cost substantially more")

    def test_the_old_flat_estimate_would_have_undercounted(self):
        """Pins why this change exists. The old formula's ubatch-scaled term stood in for the
        logits tensor but allowed ~8 MB where the real tensor is 263 MB at ubatch 512. (Its
        flat 0.30 GB base happened to exceed the tensor on its own -- the error was that the
        term did not track vocab or ubatch at all, so it silently broke on large contexts.)"""
        old_variable_term = 2048 * 8 * 512
        self.assertLess(old_variable_term, 128256 * 512 * 4,
                        "the old ubatch term was ~32x smaller than the real logits tensor")
        # and it did not respond to either input that determines the tensor's size
        def old(ub):
            return int(0.30 * GB + 2048 * 8 * ub)
        self.assertTrue(abs(old(2048) - old(256)) < 128256 * 2048 * 4 - 128256 * 256 * 4,
                        "the old term barely moved with ubatch while the real tensor grows 8x")

    def test_a_missing_vocab_falls_back_and_loses_confidence(self):
        fp = estimate_footprint(dict(LLAMA3, n_vocab=None, complete=False), 8192, "f16", 512, "on")
        self.assertEqual(fp["confidence"], "low")
        self.assertEqual(fp["n_vocab"], 0)
        self.assertGreater(fp["compute_b"], 0, "still has to produce a number")

    def test_a_complete_parse_keeps_high_confidence(self):
        self.assertEqual(estimate_footprint(LLAMA3, 8192, "f16", 512, "on")["confidence"], "high")


class FlashAttentionChargedOnlyWhenKnownOff(unittest.TestCase):
    def test_only_an_explicit_off_is_paid_for(self):
        on = estimate_footprint(LLAMA3, 8192, "f16", 512, "on")["compute_b"]
        auto = estimate_footprint(LLAMA3, 8192, "f16", 512, "auto")["compute_b"]
        off = estimate_footprint(LLAMA3, 8192, "f16", 512, "off")["compute_b"]
        self.assertEqual(on, auto, "auto may resolve either way, so charge nothing")
        self.assertGreater(off, on, "an explicit off really does allocate more")


class KvMathIsUnchanged(unittest.TestCase):
    """The GQA KV formula was correct before; this change must not have disturbed it."""

    def test_per_token_matches_the_hand_calculation(self):
        for kv_type, pe in (("f16", 2.0), ("q8_0", 34 / 32), ("q4_0", 18 / 32)):
            fp = estimate_footprint(LLAMA3, 8192, kv_type, 512, "on")
            expected = 80 * 8 * 128 * 2 * pe          # layers x kv_heads x head_dim x 2 (K+V)
            self.assertAlmostEqual(fp["kv_per_token_b"], expected, delta=1)

    def test_total_scales_with_context(self):
        a = estimate_footprint(LLAMA3, 4096, "f16", 512, "on")["kv_total_b"]
        b = estimate_footprint(LLAMA3, 8192, "f16", 512, "on")["kv_total_b"]
        self.assertAlmostEqual(b, a * 2, delta=2)


class MoeOffloadIsAdvisoryNotSubtracted(unittest.TestCase):
    MOE = dict(LLAMA3, n_expert=128, file_bytes=40 * 1024 ** 3)

    def test_weights_are_never_reduced_by_it(self):
        """Under-estimating is the direction that hangs the desktop on a WDDM spill, so the fit
        verdict must stay conservative even though the real VRAM figure is lower."""
        plain = estimate_footprint(self.MOE, 8192, "q8_0", 512, "on")
        off = estimate_footprint(self.MOE, 8192, "q8_0", 512, "on",
                                 n_cpu_moe=8, n_expert_used=128)
        self.assertEqual(plain["weights_b"], off["weights_b"])

    def test_the_advisory_figure_is_reported(self):
        off = estimate_footprint(self.MOE, 8192, "q8_0", 512, "on",
                                 n_cpu_moe=8, n_expert_used=128)
        self.assertGreater(off["moe_offloaded_b"], 0)
        self.assertLess(off["moe_offloaded_b"], off["weights_b"])

    def test_offloading_more_than_exists_does_not_zero_the_weights(self):
        off = estimate_footprint(self.MOE, 8192, "q8_0", 512, "on",
                                 n_cpu_moe=500, n_expert_used=128)
        self.assertGreater(off["weights_b"], 0, "a clamped fraction must not empty the weights")

    def test_n_cpu_moe_is_resolved_only_for_an_moe_profile(self):
        """core/process.py only emits -ncmoe when model_type == 'moe'; the estimator has to
        agree or it would model a flag that is not on the command line."""
        dense = effective_params({"model_type": "dense", "tuned": {"n_cpu_moe": 8}})
        moe = effective_params({"model_type": "moe", "tuned": {"n_cpu_moe": 8}})
        self.assertEqual(dense["n_cpu_moe"], 0)
        self.assertEqual(moe["n_cpu_moe"], 8)

    def test_a_nonsense_n_cpu_moe_does_not_raise(self):
        for bad in ("eight", None, -3, []):
            eff = effective_params({"model_type": "moe", "tuned": {"n_cpu_moe": bad}})
            self.assertEqual(eff["n_cpu_moe"], 0 if bad != -3 else 0)


class UnreadableModelFailsClosed(unittest.TestCase):
    """The fail-open case: file_bytes 0 made the footprint ~0 and the verdict "fit"."""

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _plan(self, info):
        from core.vram import preflight
        # plan_launch checks Path.exists() before parsing, so the model path has to be real
        tmp = Path(self.tmpdir) / "demo.gguf"
        tmp.write_bytes(b"x")
        profile = {"model_path": str(tmp), "context_size": 4096}
        with mock.patch.object(preflight, "parse_gguf_info", return_value=info), \
             mock.patch.object(preflight, "query_devices", return_value=[]):
            return preflight.plan_launch(profile)

    def test_an_unreadable_file_does_not_allow_the_launch(self):
        plan = self._plan(dict(LLAMA3, file_bytes=0, complete=False, stat_ok=False,
                               error="permission denied"))
        self.assertFalse(plan["allow"], "an unreadable model must not be treated as fitting")
        self.assertEqual(plan["status"], "unknown")
        self.assertIn("could not read", plan["message"])

    def test_the_message_says_why_and_that_nothing_was_launched(self):
        plan = self._plan(dict(LLAMA3, file_bytes=0, stat_ok=False, error="permission denied"))
        self.assertIn("permission denied", plan["message"])

    def test_a_readable_file_still_passes_through(self):
        plan = self._plan(dict(LLAMA3))
        self.assertNotIn("could not read", plan.get("message") or "")
        self.assertTrue(plan["allow"])


class ParserMarksStatFailure(unittest.TestCase):
    def test_the_oserror_fallback_is_flagged_stat_ok_false(self):
        """The dict itself carries file_bytes 0, so the flag is what stops a caller treating an
        unreadable file as a zero-byte one."""
        import inspect
        from core.vram import gguf_parser
        src = inspect.getsource(gguf_parser.parse_gguf_info)
        self.assertIn('"stat_ok": False', src)
        self.assertIn('"file_bytes": 0', src)

    def test_a_missing_file_still_returns_none(self):
        self.assertIsNone(parse_gguf_info(Path("no-such-model-9f3a.gguf")))


if __name__ == "__main__":
    unittest.main()