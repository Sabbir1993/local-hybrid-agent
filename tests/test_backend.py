"""tests/test_backend.py - Vulkan/CUDA device naming for the llama-server launch.

Run: python -m unittest tests.test_backend -v

core/backend.py is 17 lines and selects the whole vendor story: the `-dev <prefix><N>`
device string passed to llama-server and the visible-devices env var name. It was never
imported by any test (the only hits for "backend" were the JSON *value* "local"), so a
typo here would have surface only as a failed launch on real hardware.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import backend
from core.backend import device_prefix, visible_devices_env


class DevicePrefix(unittest.TestCase):
    def test_vulkan(self):
        self.assertEqual(device_prefix("vulkan"), "Vulkan")

    def test_cuda(self):
        self.assertEqual(device_prefix("cuda"), "CUDA")

    def test_case_insensitive(self):
        self.assertEqual(device_prefix("Vulkan"), "Vulkan")
        self.assertEqual(device_prefix("CUDA"), "CUDA")

    def test_unknown_backend_falls_back_to_vulkan(self):
        # deliberate: Vulkan is the default on the box this was built for, and a launch
        # that names a device ten times out of ten beats one that emits garbage
        for bad in ("sycl", "", None, 0):
            self.assertEqual(device_prefix(bad), "Vulkan", repr(bad))


class VisibleDevicesEnv(unittest.TestCase):
    def test_vulkan(self):
        self.assertEqual(visible_devices_env("vulkan"), "GGML_VK_VISIBLE_DEVICES")

    def test_cuda(self):
        self.assertEqual(visible_devices_env("cuda"), "CUDA_VISIBLE_DEVICES")

    def test_unknown_backend_falls_back_to_vulkan(self):
        for bad in ("sycl", "", None):
            self.assertEqual(visible_devices_env(bad), "GGML_VK_VISIBLE_DEVICES", repr(bad))

    def test_the_two_backends_disagree(self):
        # the whole point of the module: the two dicts must never collapse to one value
        self.assertNotEqual(backend.DEVICE_PREFIX["vulkan"], backend.DEVICE_PREFIX["cuda"])
        self.assertNotEqual(backend.VISIBLE_DEVICES_ENV["vulkan"],
                            backend.VISIBLE_DEVICES_ENV["cuda"])


class LaunchWiring(unittest.TestCase):
    def test_process_uses_the_prefix_for_dev(self):
        import re
        from pathlib import Path as P
        src = (P(__file__).resolve().parent.parent / "core" / "process.py").read_text(encoding="utf-8")
        self.assertIn("device_prefix(backend)", src)
        # -dev joins prefix+index per device: "Vulkan1,Vulkan2" / "CUDA0"
        self.assertTrue(re.search(r'f"\{prefix\}\{d\}"', src),
                        "expected the '-dev prefix+index' join to live in core/process.py")


if __name__ == "__main__":
    unittest.main()
