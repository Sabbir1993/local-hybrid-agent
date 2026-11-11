"""core/gpu_sysfs.py against a fake /sys tree: an Arc card behind its on-board PCIe switch, an iGPU without hwmon, and a
display connector entry. Load and energy-based power need two readings; the first one reports None, never a guess."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import gpu_sysfs


def _w(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class GpuSysfsTests(unittest.TestCase):
    def setUp(self):
        gpu_sysfs._reset_for_tests()
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.drm = base / "class" / "drm"
        self.drm.mkdir(parents=True)
        root_port = base / "devices" / "pci0000:00" / "0000:00:01.1"
        _w(root_port / "current_link_speed", "16.0 GT/s PCIe")       # the slot link
        _w(root_port / "current_link_width", "16")
        gpu = root_port / "0000:01:00.0" / "0000:02:01.0" / "0000:03:00.0"
        _w(gpu / "current_link_speed", "2.5 GT/s PCIe")              # Arc's internal switch link: not the slot
        _w(gpu / "current_link_width", "1")
        hw = gpu / "hwmon" / "hwmon3"
        _w(hw / "temp1_input", "45000")
        _w(hw / "temp1_label", "pkg")
        _w(hw / "temp2_input", "61000")
        _w(hw / "temp2_label", "vram")
        _w(hw / "energy1_input", "1000000")
        _w(hw / "power1_max", "225000000")
        self.energy = hw / "energy1_input"
        self.idle = gpu / "tile0" / "gt0" / "gtidle" / "idle_residency_ms"
        _w(self.idle, "10000")
        (self.drm / "card1").mkdir()
        os.symlink(gpu, self.drm / "card1" / "device")
        igpu = base / "devices" / "pci0000:00" / "0000:00:02.0"
        igpu.mkdir(parents=True)
        (self.drm / "card0").mkdir()
        os.symlink(igpu, self.drm / "card0" / "device")               # no hwmon: an iGPU, skipped
        (self.drm / "card1-DP-1").mkdir()                             # a connector, not a card

    def tearDown(self):
        self.tmp.cleanup()
        gpu_sysfs._reset_for_tests()

    def read_at(self, t: float) -> list:
        with mock.patch.object(gpu_sysfs.time, "monotonic", return_value=t):
            return gpu_sysfs.read_gpus(self.drm)

    def test_first_reading(self):
        rows = self.read_at(100.0)
        self.assertEqual([r["card"] for r in rows], ["card1"])
        r = rows[0]
        self.assertEqual(r["temp_c"], 45.0)                            # the pkg sensor, not the hotter vram one
        self.assertEqual(r["pcie"], "Gen4 x16")                        # slot link from the root port
        self.assertEqual(r["limit_w"], 225.0)
        self.assertIsNone(r["watts"])                                  # energy needs two readings
        self.assertIsNone(r["load_pct"])                               # idle residency needs two readings

    def test_second_reading_gives_power_and_load(self):
        self.read_at(100.0)
        _w(self.energy, str(1000000 + 150 * 2 * 10**6))                # 150 W for 2 s
        _w(self.idle, str(10000 + 500))                                # idle 0.5 s of 2 s -> 75 % busy
        r = self.read_at(102.0)[0]
        self.assertEqual(r["watts"], 150.0)
        self.assertEqual(r["load_pct"], 75.0)

    def test_calls_within_a_second_reuse_the_reading(self):
        first = self.read_at(100.0)
        _w(self.energy, "999999999")
        self.assertEqual(self.read_at(100.5), first)

    def test_no_drm_gives_nothing(self):
        self.assertEqual(gpu_sysfs.read_gpus(Path(self.tmp.name) / "missing"), [])


if __name__ == "__main__":
    unittest.main()
