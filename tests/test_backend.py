"""Backend registry, device naming, and isolated child environments."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import backend
from core.backend import (
    BackendConfigError,
    build_child_env,
    device_prefix,
    get_backend,
    validate_runtime_env,
    visible_devices_env,
)


class DevicePrefix(unittest.TestCase):
    def test_vulkan(self):
        self.assertEqual(device_prefix("vulkan"), "Vulkan")

    def test_cuda(self):
        self.assertEqual(device_prefix("cuda"), "CUDA")

    def test_sycl(self):
        self.assertEqual(device_prefix("sycl"), "SYCL")

    def test_case_insensitive(self):
        self.assertEqual(device_prefix("Vulkan"), "Vulkan")
        self.assertEqual(device_prefix("CUDA"), "CUDA")
        self.assertEqual(device_prefix("SYCL"), "SYCL")

    def test_unknown_backend_fails_closed(self):
        for bad in ("bogus", "", None, 0):
            with self.assertRaises(BackendConfigError, msg=repr(bad)):
                device_prefix(bad)


class VisibleDevicesEnv(unittest.TestCase):
    def test_vulkan(self):
        self.assertEqual(visible_devices_env("vulkan"), "GGML_VK_VISIBLE_DEVICES")

    def test_cuda(self):
        self.assertEqual(visible_devices_env("cuda"), "CUDA_VISIBLE_DEVICES")

    def test_sycl_uses_explicit_device_ids_not_an_unverified_visibility_var(self):
        self.assertIsNone(visible_devices_env("sycl"))

    def test_unknown_backend_fails_closed(self):
        for bad in ("bogus", "", None):
            with self.assertRaises(BackendConfigError, msg=repr(bad)):
                visible_devices_env(bad)

    def test_backend_mappings_remain_distinct(self):
        self.assertNotEqual(backend.DEVICE_PREFIX["vulkan"], backend.DEVICE_PREFIX["cuda"])
        self.assertNotEqual(backend.VISIBLE_DEVICES_ENV["vulkan"],
                            backend.VISIBLE_DEVICES_ENV["cuda"])


class Registry(unittest.TestCase):
    def test_all_supported_backends_exist(self):
        self.assertEqual(set(backend.BACKENDS), {"vulkan", "cuda", "sycl"})
        self.assertEqual(get_backend("SYCL").name, "sycl")

    def test_sycl_declares_multi_gpu_and_partial_offload(self):
        spec = get_backend("sycl")
        self.assertTrue(spec.multi_gpu)
        self.assertTrue(spec.tensor_split)
        self.assertTrue(spec.partial_offload)


class RuntimeEnvironment(unittest.TestCase):
    def test_vulkan_and_cuda_pin_visible_devices(self):
        base = {"PATH": "x", "CUDA_VISIBLE_DEVICES": "9",
                "GGML_VK_VISIBLE_DEVICES": "9", "ONEAPI_DEVICE_SELECTOR": "bad"}
        vk = build_child_env("vulkan", device_indices=[1, 2], base_env=base)
        self.assertEqual(vk["GGML_VK_VISIBLE_DEVICES"], "1,2")
        self.assertNotIn("CUDA_VISIBLE_DEVICES", vk)
        self.assertNotIn("ONEAPI_DEVICE_SELECTOR", vk)
        cu = build_child_env("cuda", device_indices=[0], base_env=base)
        self.assertEqual(cu["CUDA_VISIBLE_DEVICES"], "0")
        self.assertNotIn("GGML_VK_VISIBLE_DEVICES", cu)

    def test_sycl_keeps_only_sycl_runtime_controls(self):
        base = {"PATH": "x", "CUDA_VISIBLE_DEVICES": "9",
                "GGML_VK_VISIBLE_DEVICES": "9",
                "ONEAPI_DEVICE_SELECTOR": "level_zero:gpu"}
        env = build_child_env(
            "sycl",
            runtime_env={"SYCL_CACHE_PERSISTENT": "1", "SYCL_CACHE_DIR": "/cache"},
            device_indices=[0, 1],
            base_env=base,
        )
        self.assertEqual(env["ONEAPI_DEVICE_SELECTOR"], "level_zero:gpu")
        self.assertEqual(env["SYCL_CACHE_PERSISTENT"], "1")
        self.assertEqual(env["SYCL_CACHE_DIR"], "/cache")
        self.assertNotIn("CUDA_VISIBLE_DEVICES", env)
        self.assertNotIn("GGML_VK_VISIBLE_DEVICES", env)
        self.assertNotIn("ZE_AFFINITY_MASK", env)

    def test_sycl_memory_pool_switch_reaches_sycl_only(self):
        base = {"PATH": "x", "GGML_SYCL_ENABLE_VMM": "1"}
        self.assertEqual(
            validate_runtime_env("sycl", {"GGML_SYCL_ENABLE_VMM": "0"}), {"GGML_SYCL_ENABLE_VMM": "0"})
        sy = build_child_env("sycl", runtime_env={"GGML_SYCL_ENABLE_VMM": "0"}, base_env=base)
        self.assertEqual(sy["GGML_SYCL_ENABLE_VMM"], "0")          # the config value wins over the inherited one
        self.assertEqual(build_child_env("sycl", base_env=base)["GGML_SYCL_ENABLE_VMM"], "1")   # inherited when not configured
        for backend in ("vulkan", "cuda"):
            self.assertNotIn("GGML_SYCL_ENABLE_VMM", build_child_env(backend, device_indices=[0], base_env=base))
        with self.assertRaises(BackendConfigError):
            validate_runtime_env("vulkan", {"GGML_SYCL_ENABLE_VMM": "0"})

    def test_unknown_or_non_string_runtime_env_is_rejected(self):
        with self.assertRaises(BackendConfigError):
            validate_runtime_env("sycl", {"LD_PRELOAD": "/tmp/unsafe.so"})
        with self.assertRaises(BackendConfigError):
            validate_runtime_env("sycl", {"SYCL_CACHE_PERSISTENT": 1})


if __name__ == "__main__":
    unittest.main()
