"""Tensor split with more than two GPUs (up to config.MAX_GPUS).

Run: python -m unittest tests.test_multi_gpu_split -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import MAX_GPUS, normalize_tensor_split
from core.vram.estimator import _shares_for, _shares_str, compute_tensor_split
from routes.control.config_helpers import _apply_config_update

GB = 1024 ** 3


def dev(i, free_gb):
    return {"index": i, "free_b": int(free_gb * GB), "total_b": int(free_gb * GB)}


class Normalize(unittest.TestCase):
    def test_two_gpu_saved_split_unchanged(self):
        self.assertEqual(normalize_tensor_split("9,11", [1, 2]), ([1, 2], "9,11"))
        self.assertEqual(normalize_tensor_split("1,0", [1, 2]), ([1], "1"))

    def test_aligned_split_on_many_gpus_kept(self):
        devs = list(range(14))
        split = ",".join(["1"] * 14)
        self.assertEqual(normalize_tensor_split(split, devs), (devs, split))

    def test_short_split_becomes_equal_across_all_gpus(self):
        for n in (3, 4, 14):
            devs = list(range(n))
            got_devs, got = normalize_tensor_split("10,10", devs)
            self.assertEqual(got_devs, devs)
            self.assertEqual(got, ",".join(["1"] * n))

    def test_preflight_shares_match_launch_split(self):
        devs, split = normalize_tensor_split("10,10", [0, 1, 2])
        shares = _shares_for([{}] * len(devs), split)
        self.assertEqual(len(shares), 3)
        self.assertAlmostEqual(sum(shares), 1.0)


class AutoSplit(unittest.TestCase):
    def test_small_counts_keep_sum_20(self):
        self.assertEqual(compute_tensor_split([dev(0, 16), dev(1, 16)]), "10,10")

    def test_fourteen_gpus_have_resolution(self):
        got = [int(x) for x in compute_tensor_split(
            [dev(i, 24 if i < 7 else 8) for i in range(14)]).split(",")]
        self.assertEqual(len(got), 14)
        self.assertEqual(sum(got), 140)
        self.assertGreater(got[0], got[13] * 2)

    def test_shares_str_never_zero(self):
        self.assertTrue(all(int(x) >= 1 for x in _shares_str([0.9] + [0.1 / 13] * 13).split(",")))


class SaveApi(unittest.TestCase):
    def _save(self, value):
        profile = {}
        return _apply_config_update(profile, "tensor_split", value), profile

    def test_fourteen_entries_accepted(self):
        err, profile = self._save(",".join(["1"] * MAX_GPUS))
        self.assertIsNone(err)
        self.assertEqual(profile["tuned"]["tensor_split"], ",".join(["1"] * MAX_GPUS))

    def test_fifteen_entries_rejected(self):
        err, _ = self._save(",".join(["1"] * (MAX_GPUS + 1)))
        self.assertIsNotNone(err)

    def test_all_zero_rejected(self):
        err, _ = self._save("0,0,0")
        self.assertIsNotNone(err)


if __name__ == "__main__":
    unittest.main()
