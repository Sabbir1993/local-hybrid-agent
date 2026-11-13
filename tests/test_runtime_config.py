"""Strict selection of one llama.cpp runtime before application startup."""

import unittest
from unittest import mock

from core import config as config_module
from core.config import RuntimeConfigError, select_runtime
from core.vram.devices import DeviceQueryError


DEFAULTS = {
    "llama_bin_dir": "/fallback/llama-vulkan",
    "backend": "vulkan",
    "gpu_devices": [0],
}


class RuntimeSelection(unittest.TestCase):
    def test_config_selects_sycl_with_runtime_environment(self):
        app = {
            "runtime": "sycl",
            "runtimes": {
                "vulkan": {"backend": "vulkan", "llama_bin_dir": "/v", "gpu_devices": [0]},
                "sycl": {
                    "backend": "sycl",
                    "llama_bin_dir": "/var/web/html_new/local_ai/llama-sycl",
                    "gpu_devices": "auto",
                    "small_model_gpu": 1,
                    "env": {
                        "ONEAPI_DEVICE_SELECTOR": "level_zero:gpu",
                        "SYCL_CACHE_PERSISTENT": "1",
                    },
                },
            },
        }
        got = select_runtime(app, environ={}, defaults=DEFAULTS)
        self.assertEqual(got["name"], "sycl")
        self.assertEqual(got["backend"], "sycl")
        self.assertEqual(got["llama_bin_dir"], "/var/web/html_new/local_ai/llama-sycl")
        self.assertEqual(got["gpu_devices"], [0])
        self.assertTrue(got["gpu_auto"])
        self.assertEqual(got["small_model_gpu"], 1)
        self.assertEqual(got["env"]["ONEAPI_DEVICE_SELECTOR"], "level_zero:gpu")


    def test_environment_override_wins_before_startup(self):
        app = {
            "runtime": "vulkan",
            "runtimes": {
                "vulkan": {"backend": "vulkan", "llama_bin_dir": "/v", "gpu_devices": [0]},
                "sycl": {"backend": "sycl", "llama_bin_dir": "/s", "gpu_devices": [0, 1]},
            },
        }
        got = select_runtime(app, environ={"LLAMA_RUNTIME": "sycl"}, defaults=DEFAULTS)
        self.assertEqual(got["name"], "sycl")
        self.assertEqual(got["gpu_devices"], [0, 1])

    def test_missing_explicit_runtime_fails_instead_of_falling_back(self):
        app = {
            "runtime": "sycl",
            "runtimes": {
                "vulkan": {"backend": "vulkan", "llama_bin_dir": "/v", "gpu_devices": [0]},
            },
        }
        with self.assertRaisesRegex(RuntimeConfigError, "runtime 'sycl'.*not configured"):
            select_runtime(app, environ={}, defaults=DEFAULTS)

    def test_unknown_backend_fails_closed(self):
        app = {
            "runtime": "custom",
            "runtimes": {
                "custom": {"backend": "mystery", "llama_bin_dir": "/x", "gpu_devices": [0]},
            },
        }
        with self.assertRaisesRegex(RuntimeConfigError, "unknown llama.cpp backend"):
            select_runtime(app, environ={}, defaults=DEFAULTS)

    def test_invalid_devices_and_environment_are_rejected(self):
        base = {"runtime": "sycl", "runtimes": {"sycl": {
            "backend": "sycl", "llama_bin_dir": "/s", "gpu_devices": [-1]}}}
        with self.assertRaisesRegex(RuntimeConfigError, "gpu_devices"):
            select_runtime(base, environ={}, defaults=DEFAULTS)

        base["runtimes"]["sycl"]["gpu_devices"] = [0]
        base["runtimes"]["sycl"]["env"] = {"LD_PRELOAD": "/tmp/x.so"}
        with self.assertRaisesRegex(RuntimeConfigError, "not allowed"):
            select_runtime(base, environ={}, defaults=DEFAULTS)

    def test_no_runtime_table_keeps_legacy_defaults(self):
        got = select_runtime({}, environ={}, defaults=DEFAULTS)
        self.assertEqual(got["name"], "vulkan")
        self.assertEqual(got["backend"], "vulkan")
        self.assertEqual(got["gpu_devices"], [0])


class StartupValidation(unittest.TestCase):
    def test_selected_runtime_device_discovery_fails_closed_at_startup(self):
        runtime = {
            "name": "sycl", "backend": "sycl", "llama_bin_dir": "/missing",
            "gpu_devices": [0], "gpu_auto": True, "env": {},
            "small_model_gpu": 0,
        }
        with mock.patch.dict(config_module.ACTIVE_RUNTIME, runtime, clear=True), \
             mock.patch.dict(config_module.GPU_STATUS, {}, clear=True), \
             mock.patch("core.vram.devices.query_devices",
                        side_effect=DeviceQueryError("llama-bench not found")):
            with self.assertRaisesRegex(RuntimeConfigError, "device discovery failed"):
                config_module.resolve_gpu_devices(strict=True)


if __name__ == "__main__":
    unittest.main()
