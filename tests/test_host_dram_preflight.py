"""System-DRAM admission checks for llama.cpp partial offload."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.vram import preflight as pf
from core.vram.constants import GB


INFO = {
    "arch": "llama", "n_layer": 9, "n_head": 16, "n_head_kv": 4,
    "n_embd": 2048, "head_dim": 128, "ctx_train": 8192,
    "sliding_window": None, "n_expert": 0, "n_expert_used": 0,
    "n_vocab": 32000, "file_bytes": 10 * GB, "complete": True,
    "model_path": "x", "error": None,
}
DEVICE = {
    "index": 0, "id": "SYCL0", "backend": "sycl", "name": "Arc",
    "total_b": 32 * GB, "free_b": 30 * GB, "used_b": 2 * GB,
}


class HostDramGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.model = Path(self.tmp.name) / "m.gguf"
        self.model.write_bytes(b"GGUF" + b"\0" * 64)
        self.cfg = mock.patch.object(
            pf,
            "_preflight_cfg",
            return_value={"mode": "block", "headroom_gb": 1.0,
                          "host_headroom_gb": 1.0},
        )
        self.cfg.start()

    def tearDown(self):
        self.cfg.stop()
        self.tmp.cleanup()

    def _plan(self, *, available_gb, swap_used_gb=0, device_free_gb=30,
              **profile):
        prof = {
            "model_path": str(self.model), "context_size": 4096,
            "n_gpu_layers": 5, "gpu_devices": [0], "tensor_split": "1",
            "kv_cache_type": "f16", "flash_attn": "on", "ubatch_size": 128,
            "llama_bin_dir": self.tmp.name, "backend": "sycl",
            "runtime_env": {"ONEAPI_DEVICE_SELECTOR": "level_zero:gpu"},
        }
        prof.update(profile)
        snapshot = {
            "total_b": 64 * GB,
            "available_b": int(available_gb * GB),
            "swap_total_b": 16 * GB,
            "swap_used_b": int(swap_used_gb * GB),
        }
        device = dict(DEVICE, free_b=int(device_free_gb * GB))
        with mock.patch.object(pf, "parse_gguf_info", return_value=dict(INFO)), \
             mock.patch.object(pf, "query_devices", return_value=[device]), \
             mock.patch.object(pf, "host_memory_snapshot", return_value=snapshot):
            return pf.plan_launch(prof)

    def test_partial_dense_offload_accounts_for_cpu_weights_and_cache(self):
        plan = self._plan(available_gb=12)
        self.assertIn(plan["status"], ("fit", "tight"), plan["message"])
        self.assertTrue(plan["host"]["fit"])
        self.assertGreater(plan["host"]["weights_gb"], 4.5)
        self.assertEqual(plan["host"]["cache_gb"], 2.0)
        self.assertIn("system RAM", plan["message"])

    def test_vram_fit_is_rejected_when_partial_offload_does_not_fit_dram(self):
        plan = self._plan(available_gb=6)
        self.assertEqual(plan["status"], "nofit")
        self.assertFalse(plan["allow"])
        self.assertFalse(plan["host"]["fit"])
        self.assertIn("short", plan["message"])

    def test_swap_pressure_is_reported_but_not_added_to_capacity(self):
        plan = self._plan(available_gb=12, swap_used_gb=2)
        self.assertTrue(plan["host"]["fit"])
        self.assertTrue(plan["host"]["swap_warning"])
        self.assertIn("swap", plan["message"].lower())

    def test_unbounded_prompt_cache_is_rejected_for_partial_offload(self):
        plan = self._plan(available_gb=40, cache_ram=-1)
        self.assertEqual(plan["status"], "nofit")
        self.assertFalse(plan["host"]["fit"])
        self.assertIn("unbounded", plan["message"])

    def test_vram_suggestion_does_not_offer_partial_offload_that_exceeds_ram(self):
        plan = self._plan(available_gb=2, device_free_gb=6,
                          n_gpu_layers=999)
        self.assertEqual(plan["status"], "nofit")
        self.assertFalse(any(
            suggestion.get("type") == "n_gpu_layers"
            for suggestion in plan["suggestions"]
        ), plan["suggestions"])


if __name__ == "__main__":
    unittest.main()
