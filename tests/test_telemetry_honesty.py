"""tests/test_telemetry_honesty.py - the telemetry snapshot shows only what a
sensor measured.

The dashboard used to display GPU temperature and power as linear functions
of engine load ("44 + load*0.28") and a PCIe rate derived from the prefill
speed - numbers that looked like sensors and were not. These tests pin:
with no sensor source, temp/power/pcie are null end to end, VRAM total is
not defaulted to an invented 32 GB, averages are token-weighted, and the
fabrication formulas are gone from the source."""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.telemetry import _weighted_tps


class NoFabricatedFormulas(unittest.TestCase):
    def test_load_model_formulas_are_gone_from_telemetry(self):
        src = Path(Path(__file__).resolve().parents[1] / "core" / "telemetry.py") \
            .read_text(encoding="utf-8")
        for fragment in ("base_temp", "* 0.28", "base_power", "* 2.1",
                         '0.2 + (last_prefill_tps * 0.008)'):
            self.assertNotIn(fragment, src, f"fabrication fragment still present: {fragment}")

    def test_vram_total_no_longer_defaults_to_32(self):
        src = Path(Path(__file__).resolve().parents[1] / "core" / "telemetry.py") \
            .read_text(encoding="utf-8")
        self.assertNotIn("else 32.0", src)


class WeightedAverages(unittest.TestCase):
    def test_token_weighted_not_count_weighted(self):
        rows = [
            {"prompt_tokens": 1, "completion_tokens": 1, "prompt_tps": 100.0, "tps": 100.0},
            {"prompt_tokens": 4000, "completion_tokens": 500, "prompt_tps": 50.0, "tps": 10.0},
        ]
        prefill, decode = _weighted_tps(rows)
        # (1*100 + 4000*50) / 4001 tokens
        self.assertAlmostEqual(prefill, round(200100.0 / 4001, 1), delta=0.2)
        # (1*100 + 500*10) / 501 tokens
        self.assertAlmostEqual(decode, round(5100.0 / 501, 1), delta=0.2)

    def test_missing_tokens_give_zero(self):
        rows = [{"prompt_tokens": 0, "completion_tokens": None, "prompt_tps": 9.0, "tps": 9.0}]
        prefill, decode = _weighted_tps(rows)
        self.assertEqual(prefill, 0.0)
        self.assertEqual(decode, 0.0)


class SnapshotNulls(unittest.TestCase):
    def _snapshot(self, gpu_stats, sensors):
        from core import telemetry as tm
        with mock.patch.object(tm, "get_gpu_stats", mock.AsyncMock(return_value=gpu_stats)), \
             mock.patch.object(tm, "get_gpu_sensors", mock.AsyncMock(return_value=sensors)), \
             mock.patch.object(tm.state, "process", None, create=True), \
             mock.patch.object(tm, "_monitor_state", {"active": {}, "recent": []}):
            return asyncio.run(tm.get_telemetry_snapshot())

    def test_no_sensor_yields_null_temp_power_pcie(self):
        snapshot = self._snapshot(
            gpu_stats={"adapters": [], "compute": [], "vram_totals_gb": [],
                       "devices": [], "hardware_tag": "DUAL GPU · VULKAN"},
            sensors=None)
        self.assertIsNone(snapshot["cards"]["gpu_temp"]["temp_c"])
        self.assertIsNone(snapshot["cards"]["power"]["watts"])
        self.assertIsNone(snapshot["cards"]["pcie"]["rate_mb_s"])
        self.assertIsNone(snapshot["hardware"]["gpu_temp_c"])

    def test_nvidia_sensor_rows_surface(self):
        snapshot = self._snapshot(
            gpu_stats={"adapters": [{"gb": 4.0}], "compute": [],
                       "vram_totals_gb": [16], "devices": [], "hardware_tag": "t"},
            sensors={"source": "nvidia-smi", "gpus": [
                {"gpu": 0, "temp_c": 62.0, "watts": 120.0},
                {"gpu": 1, "temp_c": 58.0, "watts": 95.0},
            ]})
        self.assertEqual(snapshot["cards"]["gpu_temp"]["temp_c"], 62.0)
        self.assertEqual(snapshot["cards"]["power"]["watts"], 215.0)
        self.assertEqual(snapshot["cards"]["power"]["sensed_gpus"], 2)

    def test_sensor_with_no_readings_is_still_null(self):
        snapshot = self._snapshot(
            gpu_stats={"adapters": [{"gb": 4.0}], "compute": [],
                       "vram_totals_gb": [16], "devices": [], "hardware_tag": "t"},
            sensors={"source": "nvidia-smi", "gpus": [{"gpu": 0, "temp_c": None, "watts": None}]})
        self.assertIsNone(snapshot["cards"]["gpu_temp"]["temp_c"])
        self.assertIsNone(snapshot["cards"]["power"]["watts"])


if __name__ == "__main__":
    unittest.main()
