"""tests/test_vram_gate.py - the launch gate must fail CLOSED.

core/vram/preflight.py computed plan["allow"] on four separate paths (unreadable model,
unresolvable launch params, unparseable device list, absent target device) and then gated on
plan["status"] == "nofit" instead. Three of those four therefore printed a message and
launched anyway. These tests pin the gate on `allow`, and pin that "could not ask the driver"
is distinguishable from "this machine has no GPU".

Run: python -m unittest tests.test_vram_gate -v
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.vram import devices as dv
from core.vram import preflight as pf
from core.vram.constants import GB, PreflightError

A_DEV = {"index": 0, "name": "Fake GPU", "total_b": 16 * GB, "free_b": 15 * GB, "used_b": GB}
A_DEV2 = {"index": 1, "name": "Fake GPU 2", "total_b": 16 * GB, "free_b": 15 * GB, "used_b": GB}


def _profile(tmp: Path, **over):
    """A profile over a real (tiny) GGUF-ish file. parse_gguf_info is mocked so the estimate
    is a known shape and the test is about the GATE, not about GGUF parsing."""
    mp = tmp / "model.gguf"
    mp.write_bytes(b"GGUF" + b"\0" * 64)
    prof = {"model_path": str(mp), "context_size": 4096, "n_gpu_layers": 999,
            "gpu_devices": [0], "tensor_split": "1", "kv_cache_type": "f16",
            "flash_attn": "on", "ubatch_size": 512, "llama_bin_dir": str(tmp)}
    prof.update(over)
    return prof


_INFO = {"arch": "llama", "n_layer": 32, "n_head": 32, "n_head_kv": 8, "n_embd": 4096,
         "head_dim": 128, "ctx_train": 8192, "sliding_window": None,
         "n_expert": 0, "n_expert_used": 0, "n_vocab": 32000, "file_bytes": 2 * GB,
         "complete": True, "model_path": "x", "error": None}


def _patched(tmp, info=None, devs=(A_DEV,), **kw):
    return (
        mock.patch.object(pf, "parse_gguf_info", return_value=dict(info or _INFO)),
        mock.patch.object(pf, "query_devices", return_value=list(devs)),
    )


class GateFailsClosed(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmp.name)
        pf._preflight_cfg = lambda: {"mode": "block", "headroom_gb": 1.0}
        self._orig_cfg = pf._preflight_cfg

    def tearDown(self):
        self.tmp.cleanup()
        pf._preflight_cfg = self._orig_cfg

    def test_unreadable_model_blocks_the_launch(self):
        """The case preflight.py:69-76 was written for and never enforced."""
        bad = dict(_INFO, stat_ok=False, error="permission denied")
        p1, p2 = _patched(self.tmpdir, info=bad)
        with p1, p2:
            with self.assertRaises(PreflightError):
                pf.check_or_raise(_profile(self.tmpdir))

    def test_missing_model_file_blocks_the_launch(self):
        prof = _profile(self.tmpdir)
        prof["model_path"] = str(self.tmpdir / "nope.gguf")
        p1, p2 = _patched(self.tmpdir)
        with p1, p2:
            with self.assertRaises(PreflightError):
                pf.check_or_raise(prof)

    def test_unresolvable_launch_params_block_the_launch(self):
        p1, p2 = _patched(self.tmpdir)
        with p1, p2, mock.patch.object(pf, "effective_params",
                                      side_effect=ValueError("bad ctx")):
            with self.assertRaises(PreflightError):
                pf.check_or_raise(_profile(self.tmpdir))

    def test_device_list_could_not_be_read_blocks_the_launch(self):
        p1 = mock.patch.object(pf, "parse_gguf_info", return_value=dict(_INFO))
        p2 = mock.patch.object(pf, "query_devices",
                               side_effect=dv.DeviceQueryError("llama-bench not found"))
        with p1, p2:
            with self.assertRaises(PreflightError):
                pf.check_or_raise(_profile(self.tmpdir))

    def test_target_device_not_in_the_list_blocks_the_launch(self):
        p1, p2 = _patched(self.tmpdir, devs=(A_DEV2,))   # profile asks for index 0
        with p1, p2:
            with self.assertRaises(PreflightError):
                pf.check_or_raise(_profile(self.tmpdir))

    def test_warn_mode_lets_these_through_but_allow_stays_false(self):
        """warn/off is the documented escape hatch: load proceeds, loudly. It must NOT be a
        place where allow silently flips back to True - the plan still reports the truth."""
        pf._preflight_cfg = lambda: {"mode": "warn", "headroom_gb": 1.0}
        bad = dict(_INFO, stat_ok=False, error="permission denied")
        p1, p2 = _patched(self.tmpdir, info=bad)
        with p1, p2:
            plan = pf.check_or_raise(_profile(self.tmpdir))
        self.assertFalse(plan["allow"], "warn mode must not flip allow back to True")
        self.assertEqual(plan["status"], "unknown")

    def test_warn_mode_still_allows_a_real_fit(self):
        pf._preflight_cfg = lambda: {"mode": "warn", "headroom_gb": 1.0}
        p1, p2 = _patched(self.tmpdir)
        with p1, p2:
            self.assertTrue(pf.check_or_raise(_profile(self.tmpdir))["allow"])

    def test_a_real_fit_is_allowed(self):
        p1, p2 = _patched(self.tmpdir)
        with p1, p2:
            plan = pf.check_or_raise(_profile(self.tmpdir))
        self.assertTrue(plan["allow"])
        self.assertIn(plan["status"], ("fit", "tight"))

    def test_enforce_is_the_only_gate_and_it_reads_allow(self):
        """Direct guard against the original bug: status may be anything, allow decides."""
        plan = {"status": "unknown", "allow": False, "mode": "block", "message": "cannot size it"}
        with self.assertRaises(PreflightError):
            pf.enforce(plan)
        plan = {"status": "nofit", "allow": True, "mode": "block", "message": "did not fit"}
        self.assertIs(pf.enforce(plan), plan, "allow=True must pass even for a real nofit")


class DeviceQueryDistinguishesFailureFromEmpty(unittest.TestCase):
    def setUp(self):
        dv._dev_cache.update({"key": None, "ts": 0.0, "data": []})
        self._saved = dv._dev_cache.copy()

    def tearDown(self):
        dv._dev_cache.update(self._saved)

    def test_missing_binary_raises_in_strict_mode(self):
        with mock.patch.object(dv, "_run_list_devices",
                               side_effect=dv.DeviceQueryError("no llama-bench")):
            with self.assertRaises(dv.DeviceQueryError):
                dv.query_devices("C:/nowhere", strict=True)

    def test_missing_binary_returns_empty_in_soft_mode(self):
        """The historical contract, kept for gpu.py / state.py / specialized.py / autotune."""
        with mock.patch.object(dv, "_run_list_devices",
                               side_effect=dv.DeviceQueryError("no llama-bench")):
            self.assertEqual(dv.query_devices("C:/nowhere"), [])

    def test_failed_query_does_not_serve_a_stale_list(self):
        """It used to keep returning the previous list with an old timestamp, so a preflight
        could approve against hour-old free_b."""
        stale = [{"index": 0, "name": "old", "total_b": 8 * GB, "free_b": 7 * GB, "used_b": GB}]
        dv._dev_cache.update({"key": str(Path("C:/old").resolve()),
                              "ts": 9e9, "data": stale})
        with mock.patch.object(dv, "_run_list_devices",
                               side_effect=dv.DeviceQueryError("driver reset")):
            self.assertEqual(dv.query_devices("C:/new", force=True), [])
        self.assertEqual(dv._dev_cache["data"], [], "stale list must be dropped")

    def test_cache_is_keyed_by_bin_dir(self):
        """Switching LLAMA_RUNTIME must not serve the other backend's devices."""
        dv._dev_cache.update({"key": str(Path("C:/vulkan").resolve()), "ts": 9e9,
                              "data": [{"index": 0, "name": "vulkan", "total_b": 16 * GB,
                                        "free_b": 15 * GB, "used_b": GB}]})
        cuda = [{"index": 0, "name": "cuda", "total_b": 24 * GB, "free_b": 23 * GB, "used_b": GB}]
        with mock.patch.object(dv, "_run_list_devices", return_value=cuda):
            got = dv.query_devices("C:/cuda")
        self.assertEqual(got[0]["name"], "cuda")

    def test_unparseable_output_is_a_failure_not_zero_devices(self):
        with mock.patch.object(dv.subprocess, "run",
                               return_value=mock.Mock(stdout="Available devices:\n  Vulkan0: ???\n",
                                                      stderr="")):
            with self.assertRaises(dv.DeviceQueryError):
                dv.query_devices("C:/x", strict=True)


class SmallModelPreflightUsesRealLaunchParams(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmp.name)
        self._orig = pf._preflight_cfg
        pf._preflight_cfg = lambda: {"mode": "block", "headroom_gb": 1.0}

    def tearDown(self):
        self.tmp.cleanup()
        pf._preflight_cfg = self._orig

    def test_mmproj_bytes_are_counted(self):
        """mmproj_path used to be accepted and never read - a 1.2 GB uncounted allocation
        on the vision lane."""
        mp = self.tmpdir / "model.gguf"
        mp.write_bytes(b"GGUF" + b"\0" * 64)
        proj = self.tmpdir / "mmproj.gguf"
        proj.write_bytes(b"\0" * (900 * 1024 * 1024))     # ~900 MB
        p1 = mock.patch.object(pf, "parse_gguf_info", return_value=dict(_INFO))
        p2 = mock.patch.object(pf, "query_devices", return_value=[dict(A_DEV)])
        with p1, p2:
            without = pf.check_small_model_or_raise("vision", mp, 4096, 0)
            with_mm = pf.check_small_model_or_raise("vision", mp, 4096, 0, mmproj_path=proj)
        delta = with_mm["estimate"]["total_gb"] - without["estimate"]["total_gb"]
        self.assertGreater(delta, 0.8, "the mmproj file must be added to the estimate")

    def test_kv_cache_type_changes_the_estimate(self):
        """The lane runs -ctk q8_0; preflight used to charge f16 bytes."""
        mp = self.tmpdir / "model.gguf"
        mp.write_bytes(b"GGUF" + b"\0" * 64)
        p1 = mock.patch.object(pf, "parse_gguf_info", return_value=dict(_INFO))
        p2 = mock.patch.object(pf, "query_devices", return_value=[dict(A_DEV)])
        with p1, p2:
            f16 = pf.check_small_model_or_raise("executor", mp, 32768, 0, kv_cache_type="f16")
            q8 = pf.check_small_model_or_raise("executor", mp, 32768, 0, kv_cache_type="q8_0")
        self.assertLess(q8["estimate"]["kv_gb"], f16["estimate"]["kv_gb"])


if __name__ == "__main__":
    unittest.main()