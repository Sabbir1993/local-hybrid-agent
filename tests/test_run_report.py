"""tests/test_run_report.py - heaviest agent runs, attributed through requests.run_id.

Run: python -m unittest tests.test_run_report -v
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.db import usage
from core.sqlite_util import ThreadLocalDB


class RunReport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)   # Windows keeps the sqlite file open
        db = ThreadLocalDB(os.path.join(self.tmp.name, "u.db"))      # a scratch database, never the live usage.db
        db.execute("CREATE TABLE requests (id INTEGER PRIMARY KEY, ts REAL, endpoint TEXT, prompt_tokens INT, completion_tokens INT, "
                   "prompt_cached_tokens INT, source TEXT, run_id TEXT)")
        db.execute("CREATE TABLE route_runs (run_id TEXT PRIMARY KEY, ts REAL, user_id INT, mode TEXT, category TEXT, "
                   "steps INT, outcome TEXT, rating INT)")
        now = time.time()
        rows = [("A", 1000, "cloud"), ("A", 3000, "cloud"), ("B", 500, "local"), ("C", 9000, "cloud")]
        for run, p, src in rows:
            db.execute("INSERT INTO requests (ts, endpoint, prompt_tokens, completion_tokens, prompt_cached_tokens, source, run_id) "
                       "VALUES (?, 'agent/main', ?, 10, 0, ?, ?)", (now, p, src, run))
        db.execute("INSERT INTO requests (ts, endpoint, prompt_tokens, source, run_id) VALUES (?, 'chat/run', 99999, 'cloud', NULL)", (now,))
        db.execute("INSERT INTO requests (ts, endpoint, prompt_tokens, source, run_id) VALUES (?, 'agent/main', 77777, 'cloud', 'OLD')",
                   (now - 30 * 86400,))
        db.execute("INSERT INTO route_runs VALUES ('A', ?, 1, 'm', 'c', 2, 'answered', NULL)", (now,))
        db.commit()
        p = mock.patch.object(usage, "_usage_db", db)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_heaviest_first_with_outcome_steps_and_peak(self):
        runs = usage.db_report_runs(7, 10)
        self.assertEqual([r["run_id"] for r in runs], ["C", "A", "B"])       # no-run requests and old runs are left out
        a = runs[1]
        self.assertEqual((a["prompt_tokens"], a["peak_prompt_tokens"], a["requests"]), (4000, 3000, 2))
        self.assertEqual((a["steps"], a["outcome"]), (2, "answered"))
        self.assertEqual(a["cloud_prompt_tokens"], 4000)
        self.assertEqual(runs[2]["cloud_prompt_tokens"], 0)                  # B ran locally
        self.assertIsNone(runs[2]["outcome"])                                # no route_runs row: shown as unknown, not dropped

    def test_limit_and_window(self):
        self.assertEqual(len(usage.db_report_runs(7, 2)), 2)
        self.assertIn("OLD", [r["run_id"] for r in usage.db_report_runs(60, 10)])

    def test_a_missing_table_is_an_empty_report_not_an_error(self):
        broken = ThreadLocalDB(os.path.join(self.tmp.name, "empty.db"))
        with mock.patch.object(usage, "_usage_db", broken):
            self.assertEqual(usage.db_report_runs(7, 10), [])


if __name__ == "__main__":
    unittest.main()
