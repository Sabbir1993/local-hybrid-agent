"""tests/test_tensor_split.py - proportional tensor-split math for N GPUs.

Run: python -m unittest tests.test_tensor_split -v

core/vram/estimator.py:compute_tensor_split() turns per-device free VRAM into the
`--tensor-split` share string, scaled so the parts sum to 20. It had zero callers in the
test suite and, more importantly, zero callers in the launch path: the only production
caller is autotune.py, which uses it to *seed sweep candidates*. The launch command instead
resolves tuned -> profile -> CONFIG_DEFAULTS (today the literal "9,11").

These tests pin the function's contract so that if/when it is wired into the launch path,
the "verified with unit-level checks" claim in README:438-440 is true on its face.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.vram.estimator import _shares_str, compute_tensor_split

GB = 1024 ** 3


def dev(idx, free_gb, total_gb=None):
    return {"index": idx, "free_b": int(free_gb * GB),
            "total_b": int((total_gb if total_gb is not None else free_gb) * GB)}


class ShareString(unittest.TestCase):
    def test_parts_sum_to_scale(self):
        for norm in ([0.5, 0.5], [0.45, 0.55], [0.7, 0.2, 0.1], [1 / 3] * 3):
            parts = [int(x) for x in _shares_str(norm).split(",")]
            self.assertEqual(sum(parts), 20, norm)
            self.assertEqual(len(parts), len(norm), norm)

    def test_no_zero_parts(self):
        # a zero share disables that GPU at launch; the scaler must never mint one
        parts = [int(x) for x in _shares_str([0.97, 0.02, 0.01]).split(",")]
        self.assertTrue(all(p >= 1 for p in parts), parts)


class SplitMath(unittest.TestCase):
    def test_zero_or_one_device_is_none(self):
        self.assertIsNone(compute_tensor_split([]))
        self.assertIsNone(compute_tensor_split([dev(0, 16)]))

    def test_two_equal_cards_split_evenly(self):
        self.assertEqual(compute_tensor_split([dev(0, 16), dev(1, 16)]), "10,10")

    def test_hand_tuned_nine_eleven_reproduces(self):
        # the original 2x Arc A770 (16GB) box, with the free-VRAM figures that produced
        # the hand-tuned "9,11" shipped in CONFIG_DEFAULTS
        self.assertEqual(compute_tensor_split([dev(0, 9), dev(1, 11)]), "9,11")

    def test_split_follows_free_not_total(self):
        # same cards, different headroom: the fuller card takes the smaller share
        self.assertEqual(compute_tensor_split(
            [dev(0, 4, total_gb=16), dev(1, 12, total_gb=16)]), "5,15")

    def test_three_uneven_devices(self):
        got = compute_tensor_split([dev(0, 24), dev(1, 8), dev(2, 8)])
        parts = [int(x) for x in got.split(",")]
        self.assertEqual(len(parts), 3)
        self.assertEqual(sum(parts), 20)
        self.assertGreater(parts[0], parts[1])
        self.assertEqual(parts[1], parts[2])

    def test_degenerate_zero_free_is_clamped_not_crash(self):
        got = compute_tensor_split([{"index": 0, "free_b": 0}, {"index": 1, "free_b": 0}])
        self.assertEqual(got, "10,10")

    def test_missing_free_falls_back_to_total(self):
        got = compute_tensor_split([{"index": 0, "total_b": 8 * GB},
                                    {"index": 1, "total_b": 24 * GB}])
        parts = [int(x) for x in got.split(",")]
        self.assertGreater(parts[1], parts[0])


class LaunchPathStatus(unittest.TestCase):
    def test_split_is_consumed_by_autotune_not_by_launch(self):
        """Pins the actual wiring, because README:189-192 describes this function as the
        launch mechanism. core/process.py resolves tuned -> profile -> CONFIG_DEFAULTS and
        never calls compute_tensor_split; autotune.py is the only production caller and uses
        it to seed sweep candidates over real llm-bench runs.

        If this function is ever wired into build_launch_command, this is the test to update -
        and the README sentence to fix alongside it.
        """
        from pathlib import Path as P
        repo = P(__file__).resolve().parent.parent
        process_src = (repo / "core" / "process.py").read_text(encoding="utf-8")
        autotune_src = (repo / "autotune.py").read_text(encoding="utf-8")
        self.assertNotIn("compute_tensor_split", process_src)
        self.assertIn("compute_tensor_split", autotune_src)


if __name__ == "__main__":
    unittest.main()
