"""Helpers that do not pass -dev (whisper.cpp, sd.cpp) must still start: the loader used to read the device name
from the launch command and failed with "list.index(x): x not in list"."""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import small_model  # noqa: E402
from core.small_model import specialized  # noqa: E402
from core.small_model.instance import SmallModelInstance  # noqa: E402


def _bare(cls, gpu):
    inst = object.__new__(cls)
    inst.gpu = gpu
    return inst


class DeviceLabel(unittest.TestCase):
    def test_a_dev_option_names_the_device(self):
        inst = _bare(SmallModelInstance, 2)
        self.assertEqual(inst._device_label(["llama-server", "-dev", "SYCL1", "--port", "8093"]), "SYCL1")

    def test_without_dev_the_configured_gpu_or_cpu_is_named(self):
        self.assertEqual(_bare(SmallModelInstance, 2)._device_label(["whisper-server", "--port", "8096"]), "GPU 2")
        self.assertEqual(_bare(SmallModelInstance, -1)._device_label(["whisper-server", "-ng"]), "CPU")

    def test_a_dev_option_with_no_value_does_not_raise(self):
        self.assertEqual(_bare(SmallModelInstance, 0)._device_label(["x", "-dev"]), "GPU 0")


class WhisperLoadsWithoutDev(unittest.TestCase):
    def test_the_loader_gets_past_the_device_label(self):
        inst = object.__new__(specialized.WhisperInstance)
        inst.process = None
        inst.lock = asyncio.Lock()
        inst.role, inst.gpu, inst.port, inst.threads = "stt", 2, 8096, 4
        inst.model_path = Path("ggml-large-v3.bin")
        inst.last_used = 0

        class Stop(Exception):
            pass

        async def go():
            with mock.patch.object(specialized.WhisperInstance, "available", new_callable=mock.PropertyMock, return_value=True), \
                    mock.patch.object(specialized, "_get_whisper_server", return_value=Path("whisper-server")), \
                    mock.patch("subprocess.Popen", side_effect=Stop("reached Popen")):
                await inst.ensure_loaded()

        with self.assertRaises(Stop):               # a ValueError here is the bug: it fired before the process was started
            asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
