"""Tensor split and partial-offload tuning is isolated by native runtime."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import config
from core.profiles import config_store


class ApplyRuntimeTuning(unittest.TestCase):
    def _apply(self, runtime, profile):
        active = {
            "name": runtime, "backend": runtime,
            "llama_bin_dir": f"/{runtime}", "gpu_devices": [0, 1], "env": {},
        }
        with mock.patch.dict(config.ACTIVE_RUNTIME, active, clear=True):
            return config.apply_runtime(profile)

    def test_selected_runtime_gets_only_its_tuning(self):
        profile = {
            "tuned": {"tensor_split": "10,10", "n_gpu_layers": 99},
            "runtime_tuned": {
                "vulkan": {"tensor_split": "9,11", "n_gpu_layers": 80},
                "sycl": {"tensor_split": "8,12", "n_gpu_layers": 60,
                         "n_cpu_moe": 4},
            },
        }
        got = self._apply("sycl", profile)
        self.assertEqual(got["tuned"], {
            "tensor_split": "8,12", "n_gpu_layers": 60, "n_cpu_moe": 4,
        })

    def test_missing_runtime_entry_does_not_reuse_another_runtime(self):
        profile = {
            "tuned": {"tensor_split": "9,11", "n_gpu_layers": 80},
            "tensor_split": "9,11",
            "n_gpu_layers": 80,
            "runtime_tuned": {"vulkan": {"tensor_split": "9,11"}},
        }
        got = self._apply("sycl", profile)
        self.assertEqual(got["tuned"], {})
        self.assertNotIn("tensor_split", got)
        self.assertNotIn("n_gpu_layers", got)


class PersistRuntimeTuning(unittest.TestCase):
    def test_saving_sycl_preserves_vulkan_and_adds_sycl_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "model_configs.json"
            path.write_text(json.dumps({
                "m.gguf": {
                    "runtime_tuned": {
                        "vulkan": {"tensor_split": "9,11", "n_gpu_layers": 80}
                    }
                }
            }), encoding="utf-8")
            profile = {
                "runtime_name": "sycl",
                "tuned": {"tensor_split": "8,12", "n_gpu_layers": 60,
                          "n_cpu_moe": 4},
            }
            with mock.patch.object(config_store, "MODEL_CONFIGS_FILE", path):
                config_store.save_model_config("m.gguf", profile)
                saved = json.loads(path.read_text(encoding="utf-8"))["m.gguf"]
        self.assertEqual(saved["runtime_tuned"]["vulkan"]["tensor_split"], "9,11")
        self.assertEqual(saved["runtime_tuned"]["sycl"]["tensor_split"], "8,12")
        self.assertEqual(saved["runtime_tuned"]["sycl"]["n_cpu_moe"], 4)


if __name__ == "__main__":
    unittest.main()
