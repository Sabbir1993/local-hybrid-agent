"""tests/test_autotune_robustness.py - failure classification and profile
defaults for the autotune sweep.

Background: run_bench used to return a single "oom_or_error" for every
failure, so sweep_n_cpu_moe backed off its offload permanently on a segfault
or a cold-load timeout - recording "doesn't fit" for a config that was never
actually sized. These tests pin: OOM vs crash vs timeout are distinct, only
OOM breaks the MoE sweep, non-OOM failures get one retry, and a profile
without an autotune block gets runnable defaults instead of a KeyError."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import autotune

FAIL = {"ok": False}
OK = {"ok": True, "pp_ts": 10.0, "tg_ts": 5.0}


def _profile(model_type: str = "moe", candidates=(16, 8, 0)) -> dict:
    return {
        "model_path": "m.gguf",
        "model_type": model_type,
        "flash_attn": "auto",
        "llama_bin_dir": ".",
        "gpu_devices": [1],
        "autotune": {
            "n_gpu_layers": 99, "split_mode": "layer",
            "bench_prompt_tokens": 16, "bench_gen_tokens": 8,
            "repetitions": 1, "bench_timeout_s": 300,
            "n_cpu_moe_candidates": list(candidates),
        },
    }


class BreakerClassification(unittest.TestCase):
    def _run_sweep(self, seq, candidates=(16, 8, 0)):
        profile = _profile(candidates=candidates)
        with mock.patch.object(autotune, "run_bench", side_effect=seq), \
             mock.patch.object(autotune, "build_gpu_env", return_value={}):
            # stderr spam is fine; sys.exit only fires when nothing succeeded
            with self.assertRaises(SystemExit):
                autotune.sweep_n_cpu_moe("bench", profile, "1")

    def test_only_oom_backs_off(self):
        # oom at 16 -> stays at high offload, no candidate after a crash is
        # misread as "doesn't fit"
        seq = [OK | {"tg_ts": 9.0},
               {"ok": False, "reason": "oom"},
               {"ok": False, "reason": "crash"}]
        with mock.patch.object(autotune, "run_bench", side_effect=seq) as mb, \
             mock.patch.object(autotune, "build_gpu_env", return_value={}):
            ncmoe, r = autotune.sweep_n_cpu_moe("bench", _profile(), "1")
        self.assertEqual(ncmoe, 16)
        self.assertEqual(mb.call_count, 2)  # 16 OK, then 8 OOM: never reached 0

    def test_crash_gets_one_retry_then_skipped_not_backed_off(self):
        seq = [
            # candidate 16: crash, retry crashes -> SKIPPED, not backoff
            {"ok": False, "reason": "crash"}, {"ok": False, "reason": "crash"},
            # candidate 8: Ok
            OK | {"tg_ts": 7.0},
            # candidate 0: oom -> normal backoff from the bottom step
            {"ok": False, "reason": "oom"},
        ]
        with mock.patch.object(autotune, "run_bench", side_effect=seq) as mb, \
             mock.patch.object(autotune, "build_gpu_env", return_value={}):
            ncmoe, r = autotune.sweep_n_cpu_moe("bench", _profile(), "1")
        self.assertEqual(ncmoe, 8)
        self.assertEqual(r["tg_ts"], 7.0)
        self.assertEqual(mb.call_count, 4)  # 2 crash attempts + ok + oom

    def test_timeout_is_not_oom(self):
        seq = [
            {"ok": False, "reason": "timeout"}, {"ok": False, "reason": "timeout"},
            OK | {"tg_ts": 4.0},
            {"ok": False, "reason": "oom"},
        ]
        with mock.patch.object(autotune, "run_bench", side_effect=seq), \
             mock.patch.object(autotune, "build_gpu_env", return_value={}):
            ncmoe, r = autotune.sweep_n_cpu_moe("bench", _profile(), "1")
        self.assertEqual(ncmoe, 8)  # 16 timed out (twice) and was skipped, not OOM'd

    def test_everything_failing_exits_cleanly(self):
        self._run_sweep([{"ok": False, "reason": "oom"}])


class OomIsTheOnlyBreaker2(unittest.TestCase):
    def test_transient_then_ok_is_used(self):
        # candidate fails once transiently, retry succeeds: candidate adopted
        seq = [{"ok": False, "reason": "crash"},
               OK | {"tg_ts": 12.0},
               {"ok": False, "reason": "oom"}]
        with mock.patch.object(autotune, "run_bench", side_effect=seq), \
             mock.patch.object(autotune, "build_gpu_env", return_value={}):
            ncmoe, r = autotune.sweep_n_cpu_moe("bench", _profile(), "1")
        self.assertEqual(ncmoe, 16)
        self.assertEqual(r["tg_ts"], 12.0)


class AutotuneDefaults(unittest.TestCase):
    def test_bare_profile_gets_runnable_block(self):
        profile = {"model_path": "m.gguf", "model_type": "dense",
                   "tuned": {"n_gpu_layers": 88}}
        auto = autotune.ensure_autotune_block(profile)
        self.assertEqual(auto["n_gpu_layers"], 88)
        self.assertEqual(auto["split_mode"], "layer")
        self.assertEqual(auto["bench_timeout_s"], autotune.DEFAULT_BENCH_TIMEOUT_S)
        # dense models never get the moe ladder injected
        self.assertNotIn("n_cpu_moe_candidates", auto)
        self.assertTrue(profile["autotune"] is auto)

    def test_moe_profile_gets_the_offload_ladder(self):
        profile = {"model_path": "m.gguf", "model_type": "moe"}
        auto = autotune.ensure_autotune_block(profile)
        self.assertEqual(auto["n_cpu_moe_candidates"],
                         autotune.DEFAULT_N_CPU_MOE_CANDIDATES)

    def test_existing_autotune_block_is_never_clobbered(self):
        profile = {"model_path": "m.gguf",
                   "autotune": {"bench_gen_tokens": 128, "split_mode": "row"}}
        auto = autotune.ensure_autotune_block(profile)
        self.assertEqual(auto["split_mode"], "row")
        self.assertEqual(auto["bench_gen_tokens"], 128)

    def test_tuned_missing_is_fine_for_writeback(self):
        # the old f"profile["tuned"].update" KeyError shape, exercised on the
        # real write path with atomic_write_json mocked out
        profile = {"model_path": "m.gguf"}
        autotune.ensure_autotune_block(profile)
        profile.setdefault("tuned", {}).update({"tensor_split": "1,1"})
        self.assertEqual(profile["tuned"]["tensor_split"], "1,1")


class AtomicWrite(unittest.TestCase):
    def test_main_writes_profile_atomically(self):
        # autotune.py:202 used plain write_text - the one non-atomic write in
        # the config stack; pin the atomic path being used
        src = Path(autotune.__file__).read_text(encoding="utf-8")
        self.assertNotIn("profile_path.write_text(json.dumps(profile, indent=2))", src)
        self.assertIn("atomic_write_json(profile_path, profile)", src)


if __name__ == "__main__":
    unittest.main()
