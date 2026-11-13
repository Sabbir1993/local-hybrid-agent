"""Runtime-aware llama-bench environment and exact multi-GPU selection."""

import unittest
from pathlib import Path
from unittest import mock

from core.llama_bench import build_gpu_env, run_bench


class BenchEnvironment(unittest.TestCase):
    def test_sycl_environment_does_not_inherit_other_backend_selectors(self):
        profile = {
            "backend": "sycl",
            "gpu_devices": [0, 1],
            "runtime_env": {"ONEAPI_DEVICE_SELECTOR": "level_zero:gpu",
                            "SYCL_CACHE_PERSISTENT": "1"},
        }
        with mock.patch.dict("os.environ", {
            "CUDA_VISIBLE_DEVICES": "7",
            "GGML_VK_VISIBLE_DEVICES": "8",
        }, clear=True):
            env = build_gpu_env(profile)
        self.assertEqual(env["ONEAPI_DEVICE_SELECTOR"], "level_zero:gpu")
        self.assertEqual(env["SYCL_CACHE_PERSISTENT"], "1")
        self.assertNotIn("CUDA_VISIBLE_DEVICES", env)
        self.assertNotIn("GGML_VK_VISIBLE_DEVICES", env)


class BenchCommand(unittest.TestCase):
    def test_exact_device_ids_are_passed_in_configured_order(self):
        completed = mock.Mock(
            returncode=0,
            stdout='[{"n_prompt": 0, "n_gen": 16, "avg_ts": 5.0}]',
            stderr="",
        )
        with mock.patch("core.llama_bench.subprocess.run", return_value=completed) as run:
            result = run_bench(
                Path("/runtime/llama-bench"),
                "/models/m.gguf",
                ngl=40,
                split_mode="layer",
                tensor_split="9,11",
                n_cpu_moe=4,
                flash_attn="auto",
                n_prompt=16,
                n_gen=16,
                repetitions=1,
                device_ids=["SYCL2", "SYCL0"],
                env={"PATH": "x"},
            )
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[cmd.index("-dev") + 1], "SYCL2,SYCL0")
        self.assertEqual(cmd[cmd.index("--tensor-split") + 1], "9/11")
        self.assertEqual(cmd[cmd.index("-ncmoe") + 1], "4")
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
