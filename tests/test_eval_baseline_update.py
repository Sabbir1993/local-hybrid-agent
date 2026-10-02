"""tests/test_eval_baseline_update.py - --update-baseline must not be a silent no-op.

RED for T1 follow-up: the update block sat INSIDE `if args.mock:`, so the
documented `python tests/eval_agent.py --update-baseline` (offline-only, which
is how you would re-baseline the offline suite) skipped the write entirely - no
error, no output, and the stale FAIL in tests/eval_baseline.json survived
forever. That is how `compact_endpoint` stayed FAIL in the baseline long after
the check itself was fixed.

Both modes are pinned here:
  * offline-only update writes the offline records it just measured
  * a mock update still merges, and never drops sections it did not measure
"""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

import eval_agent  # noqa: E402
import eval_mock  # noqa: E402


class OfflineOnlyUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="baseline_update_")
        self.base = Path(self.tmp.name)
        self.baseline = self.base / "baseline.json"
        self.results = self.base / "results.json"
        # a baseline that already carries the other sections, so the merge
        # behaviour is observable rather than assumed
        self.baseline.write_text(json.dumps({
            "tasks": {"t1": {"pass_rate": 1.0, "n": 3}},
            "aggregate_rate": 1.0,
            "retrieval": {"lexonly_recall_at_k": 1.0, "hybrid_real_recall_at_k": 1.0},
            "router": {"ok": True},
            "code": {"recall": 1.0},
            "offline": {"compact_endpoint": "FAIL"},
        }), encoding="utf-8")
        self._patches = [
            mock.patch.object(eval_mock, "BASELINE_FILE", self.baseline),
            mock.patch.object(eval_agent, "RESULTS_FILE", self.results),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def test_offline_only_update_writes_the_offline_records(self):
        # no --mock: this invocation measures ONLY the offline checks
        with mock.patch.object(sys, "argv", ["eval_agent.py", "--update-baseline"]):
            eval_agent.main()
        written = json.loads(self.baseline.read_text(encoding="utf-8"))
        self.assertIsNotNone(written.get("offline"),
                             "offline-only update wrote nothing - silent no-op")
        self.assertIn("compact_endpoint", written["offline"])
        self.assertEqual(written["offline"]["compact_endpoint"], "PASS")

    def test_offline_only_update_keeps_sections_it_did_not_measure(self):
        with mock.patch.object(sys, "argv", ["eval_agent.py", "--update-baseline"]):
            eval_agent.main()
        written = json.loads(self.baseline.read_text(encoding="utf-8"))
        self.assertEqual(written["retrieval"]["hybrid_real_recall_at_k"], 1.0,
                         "a section this run did not measure must survive")
        self.assertEqual(written["code"], {"recall": 1.0})
        self.assertEqual(written["tasks"], {"t1": {"pass_rate": 1.0, "n": 3}})
        self.assertEqual(written["aggregate_rate"], 1.0,
                         "an offline-only update must not un-gate the mock aggregate")


class MockUpdateKeepsMergingTests(unittest.TestCase):
    def test_code_and_offline_sections_survive_a_mock_update(self):
        with tempfile.TemporaryDirectory(prefix="baseline_mock_") as tmp:
            base = Path(tmp)
            baseline = base / "baseline.json"
            results = base / "results.json"
            baseline.write_text(json.dumps({
                "tasks": {}, "aggregate_rate": 1.0,
                "retrieval": {"lexonly_recall_at_k": 1.0, "hybrid_real_recall_at_k": 1.0},
                "router": {"ok": True}, "code": {"recall": 1.0},
                "offline": {"compact_endpoint": "PASS"},
            }), encoding="utf-8")
            with mock.patch.object(eval_mock, "BASELINE_FILE", baseline), \
                 mock.patch.object(eval_agent, "RESULTS_FILE", results), \
                 mock.patch.object(sys, "argv", ["eval_agent.py", "--mock", "--no-offline",
                                                 "--no-retrieval", "--no-router", "--no-code",
                                                 "--update-baseline", "--repeats", "1"]):
                eval_agent.main()
            written = json.loads(baseline.read_text(encoding="utf-8"))
            self.assertEqual(written["retrieval"]["hybrid_real_recall_at_k"], 1.0)
            self.assertEqual(written["code"], {"recall": 1.0})
            self.assertEqual(written["offline"], {"compact_endpoint": "PASS"},
                             "--no-offline must not erase the recorded offline section")


if __name__ == "__main__":
    unittest.main()