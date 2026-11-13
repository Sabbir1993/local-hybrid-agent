"""Backend-neutral GPU status and preflight diagnostics."""

import unittest
from unittest import mock

from core import gpu
from core.vram import GB
from core.vram.preflight import _human_msg


class LinuxGpuStatus(unittest.TestCase):
    def test_sycl_inventory_ids_are_used_as_adapter_ids(self):
        devs = [{
            "index": 2,
            "id": "SYCL2",
            "backend": "sycl",
            "name": "Intel Arc A770",
            "total_b": 16 * GB,
            "free_b": 10 * GB,
            "used_b": 6 * GB,
        }]
        with mock.patch.object(gpu.vram, "query_devices", return_value=devs), \
             mock.patch.dict(gpu.ACTIVE_RUNTIME, {"backend": "sycl"}, clear=False):
            got = gpu._query_gpu_linux()
        self.assertEqual(got["adapters"], [{"luid": "sycl2", "gb": 6.0}])

    def test_hardware_summary_uses_active_runtime_not_legacy_top_level_config(self):
        devs = [{
            "index": 0, "id": "SYCL0", "backend": "sycl", "name": "Arc",
            "total_b": 16 * GB, "free_b": 15 * GB, "used_b": GB,
        }]
        with mock.patch.object(gpu.vram, "query_devices", return_value=devs), \
             mock.patch.dict(gpu.ACTIVE_RUNTIME, {"backend": "sycl"}, clear=False):
            got = gpu._compute_hardware_engine_summary()
        self.assertEqual(got["engine"], "SYCL")
        self.assertEqual(got["hardware_tag"], "SINGLE GPU · SYCL")
        self.assertEqual(got["devices"][0]["id"], "SYCL0")


class PreflightMessage(unittest.TestCase):
    def test_message_names_exact_sycl_device(self):
        plan = {
            "status": "nofit",
            "model": "m.gguf",
            "estimate": {"total_gb": 20, "weights_gb": 18, "kv_gb": 1,
                         "compute_gb": 1, "headroom_gb": 1},
            "effective": {"n_gpu_layers": 40, "tensor_split": "1"},
            "devices": [{"index": 2, "id": "SYCL2", "target": True,
                         "need_gb": 20, "free_gb": 15, "short_gb": 5}],
            "suggestions": [],
        }
        msg = _human_msg(plan)
        self.assertIn("SYCL2: need 20 GB", msg)
        self.assertNotIn("Vulkan2", msg)


if __name__ == "__main__":
    unittest.main()
