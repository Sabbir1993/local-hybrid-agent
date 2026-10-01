"""tests/test_eval_isolation.py - MockEvalEnv is re-runnable in-process.

Tuning sweeps run the retrieval slice several times per process. The memory
DB globals (store + package connections) and the search cache used to survive
__exit__, so run 2 measured run 1's deleted temp files and returned all-empty
(recall 0.0, fallout 0.0, no exceptions - maximally misleading). This pins
repeatability: three consecutive slices, identical numbers.

Run: python -m unittest tests.test_eval_isolation -v
"""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from eval_mock import run_retrieval_slice


class SliceRerunTests(unittest.TestCase):
    def test_three_consecutive_slices_agree(self):
        seen = []
        for _ in range(3):
            rec = asyncio.run(run_retrieval_slice(embedder="fake"))
            self.assertFalse(rec["problems"], rec["problems"])
            seen.append((rec["lexonly_recall_at_k"], rec["lexonly_fallout_at_k"],
                         rec["recall_at_k"], rec["fallout_at_k"]))
        self.assertEqual(seen[0], seen[1])
        self.assertEqual(seen[0], seen[2])
        # And the numbers are the live ones, not the all-empty failure mode.
        self.assertGreater(seen[0][0], 0.6)


if __name__ == "__main__":
    unittest.main()
