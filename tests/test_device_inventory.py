"""Backend-neutral llama-bench device inventory and exact ID mapping."""

import unittest
from unittest import mock

from core.vram import devices


class ParseDeviceList(unittest.TestCase):
    def test_vulkan_cuda_and_sycl_preserve_exact_ids(self):
        output = """
Available devices:
  Vulkan0: Intel Arc A770 (16368 MiB, 15000 MiB free)
  CUDA1: NVIDIA L40S (48 GiB, 47.5 GiB free)
  SYCL2: Intel(R) Data Center GPU Max 1550 (65536 MiB, 64000 MiB free)
"""
        got = devices.parse_device_list(output)
        self.assertEqual([d["id"] for d in got], ["Vulkan0", "CUDA1", "SYCL2"])
        self.assertEqual([d["backend"] for d in got], ["vulkan", "cuda", "sycl"])
        self.assertEqual([d["index"] for d in got], [0, 1, 2])
        self.assertGreater(got[2]["free_b"], got[0]["free_b"])

    def test_expected_backend_filters_other_device_families(self):
        output = "Vulkan0: A (8 GiB, 7 GiB free)\nSYCL0: B (16 GiB, 15 GiB free)"
        got = devices.parse_device_list(output, expected_backend="sycl")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["id"], "SYCL0")

    def test_unparseable_output_raises_with_bounded_context(self):
        with self.assertRaisesRegex(devices.DeviceQueryError, "no parseable device lines"):
            devices.parse_device_list("Available devices:\nSYCL?: unknown")


class ResolveDeviceIds(unittest.TestCase):
    INVENTORY = [
        {"index": 0, "id": "SYCL0", "backend": "sycl"},
        {"index": 2, "id": "SYCL2", "backend": "sycl"},
    ]

    def test_config_order_becomes_exact_llama_ids(self):
        self.assertEqual(
            devices.resolve_device_ids([2, 0], backend="sycl", inventory=self.INVENTORY),
            ["SYCL2", "SYCL0"],
        )

    def test_missing_inventory_entry_fails_when_strict(self):
        with self.assertRaisesRegex(devices.DeviceQueryError, "device index 1"):
            devices.resolve_device_ids([1], backend="sycl", inventory=self.INVENTORY,
                                       strict=True)

    def test_offline_compatibility_uses_validated_backend_prefix(self):
        self.assertEqual(
            devices.resolve_device_ids([3], backend="sycl", inventory=[], strict=False),
            ["SYCL3"],
        )


class DiscoveryEnvironment(unittest.TestCase):
    def test_list_devices_uses_only_the_active_runtime_environment(self):
        completed = mock.Mock(
            returncode=0,
            stdout="SYCL0: Intel Arc A770 (16 GiB, 15 GiB free)\n",
            stderr="",
        )
        with mock.patch.object(devices.Path, "exists", return_value=True), \
             mock.patch.object(devices.subprocess, "run", return_value=completed) as run:
            got = devices._run_list_devices(
                "/runtime",
                backend="sycl",
                runtime_env={"ONEAPI_DEVICE_SELECTOR": "level_zero:gpu"},
            )
        self.assertEqual(got[0]["id"], "SYCL0")
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["ONEAPI_DEVICE_SELECTOR"], "level_zero:gpu")
        self.assertNotIn("CUDA_VISIBLE_DEVICES", env)
        self.assertNotIn("GGML_VK_VISIBLE_DEVICES", env)


if __name__ == "__main__":
    unittest.main()
