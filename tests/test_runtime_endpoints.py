"""Tests for the machine-level runtime settings endpoint.

The real app.json is never touched: update_app_config and APP_CONFIG are replaced
with in-memory copies, and the audit write is stubbed.
"""

import asyncio
import copy
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from routes.control import runtime_endpoints as rt


BASE = {
    "runtime": "vulkan",
    "runtimes": {
        "vulkan": {
            "backend": "vulkan",
            "llama_bin_dir": r"D:\AI\vulkan-arc\llama-vulkan",
            "gpu_devices": [1, 2],
            "small_model_gpu": 1,
        },
        "cuda": {
            "backend": "cuda",
            "llama_bin_dir": r"D:\AI\cuda-arc\llama-cuda",
            "gpu_devices": [0],
        },
    },
}


class DummyUser:
    id = 1
    email = "admin@example.test"
    username = "admin"


REQUEST = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))


def _apply_to_base(mutator):
    candidate = copy.deepcopy(BASE)
    mutator(candidate)
    return candidate


def call_put(**kwargs):
    return asyncio.run(rt.set_runtime(rt.RuntimeSettingsRequest(**kwargs), REQUEST, DummyUser()))


class RuntimeEndpointTests(unittest.TestCase):
    def setUp(self):
        self.patches = [
            patch.object(rt, "APP_CONFIG", copy.deepcopy(BASE)),
            patch.object(rt, "update_app_config", side_effect=_apply_to_base),
            patch.object(rt, "audit_log"),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(self.stop_patches)

    def stop_patches(self):
        for p in reversed(self.patches):
            p.stop()

    def test_get_snapshot(self):
        d = asyncio.run(rt.get_runtime(DummyUser()))
        self.assertEqual(d["runtime"], "vulkan")
        self.assertEqual(set(d["runtimes"]), {"vulkan", "cuda"})
        self.assertTrue({"vulkan", "cuda", "sycl"} <= set(d["available_backends"]))
        self.assertTrue(d["requires_restart"])
        self.assertEqual(d["active"]["backend"], "vulkan")
        self.assertIsNone(d["active"]["error"])

    def test_reject_unknown_backend(self):
        with self.assertRaises(HTTPException) as cm:
            call_put(backend="bogus")
        self.assertEqual(cm.exception.status_code, 400)

    def test_reject_bad_gpu_devices(self):
        for bad in ([0, 99], [1, 1], "abc", [], [-1]):
            with self.subTest(bad=bad), self.assertRaises(HTTPException) as cm:
                call_put(gpu_devices=bad)
            self.assertEqual(cm.exception.status_code, 400)

    def test_reject_empty_bin_dir(self):
        with self.assertRaises(HTTPException) as cm:
            call_put(llama_bin_dir="   ")
        self.assertEqual(cm.exception.status_code, 400)

    def test_save_updates_preset_and_live_config(self):
        out = call_put(runtime="vulkan", backend="sycl", gpu_devices="auto", small_model_gpu=2)
        self.assertTrue(out["ok"])
        self.assertTrue(out["requires_restart"])
        self.assertEqual(out["active"]["backend"], "sycl")
        self.assertEqual(out["active"]["gpu_devices"], [1, 2])  # "auto" resolved at startup
        self.assertEqual(rt.APP_CONFIG["runtime"], "vulkan")
        self.assertEqual(rt.APP_CONFIG["runtimes"]["vulkan"]["backend"], "sycl")
        self.assertEqual(rt.APP_CONFIG["runtimes"]["vulkan"]["gpu_devices"], "auto")
        self.assertEqual(rt.APP_CONFIG["runtimes"]["vulkan"]["small_model_gpu"], 2)
        rt.audit_log.assert_called_once()

    def test_new_preset_starts_from_shipped_defaults(self):
        out = call_put(runtime="sycl", backend="sycl",
                       llama_bin_dir=r"D:\AI\sycl-arc\llama-sycl", gpu_devices=[0, 1])
        self.assertTrue(out["ok"])
        preset = rt.APP_CONFIG["runtimes"]["sycl"]
        self.assertEqual(preset["backend"], "sycl")
        self.assertEqual(preset["gpu_devices"], [0, 1])
        self.assertEqual(rt.APP_CONFIG["runtime"], "sycl")

    def test_env_override_is_reported(self):
        with patch.dict(os.environ, {"LLAMA_RUNTIME": "cuda"}):
            d = asyncio.run(rt.get_runtime(DummyUser()))
            self.assertEqual(d["env_override"], "cuda")
            out = call_put(backend="vulkan")
            self.assertIn("warning", out)
            self.assertIn("LLAMA_RUNTIME", out["warning"])


if __name__ == "__main__":
    unittest.main()
