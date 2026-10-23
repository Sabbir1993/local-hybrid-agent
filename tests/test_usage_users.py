"""Per-user token consumption (usage report "By user" tab): grouping, run-based attribution, record_request default user."""
import sqlite3
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.db import usage  # noqa: E402
from core.request_context import set_current_user  # noqa: E402


def _db() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.execute("""CREATE TABLE requests (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, endpoint TEXT, model TEXT,
        prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER, tps REAL, duration_s REAL, prompt_tps REAL,
        stream INTEGER, status INTEGER, prompt_cached_tokens INTEGER DEFAULT 0, completion_cached_tokens INTEGER DEFAULT 0,
        is_orchestrator INTEGER DEFAULT 0, source TEXT, provider TEXT, run_id TEXT, user_id INTEGER)""")
    c.execute("CREATE TABLE route_runs (run_id TEXT PRIMARY KEY, ts REAL, user_id INTEGER)")
    return c


class UserReportTests(unittest.TestCase):
    def setUp(self):
        self.db = _db()
        self._p = mock.patch.object(usage, "_usage_db", self.db)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        set_current_user(None)

    def _row(self, user, model, total, source="local", run=None, ts=None):
        self.db.execute("INSERT INTO requests (ts, endpoint, model, prompt_tokens, completion_tokens, total_tokens, source, "
                        "run_id, user_id) VALUES (?,?,?,?,?,?,?,?,?)",
                        (ts or time.time(), "chat/run", model, total - 10, 10, total, source, run, user))

    def test_groups_by_user_with_top_model_cloud_share_and_unattributed(self):
        self._row(1, "small", 100)
        self._row(1, "big", 900, source="cloud")
        self._row(2, "small", 50)
        self._row(None, "small", 5)
        rows = usage.db_report_users(7)
        by = {r["user_id"]: r for r in rows}
        self.assertEqual([r["user_id"] for r in rows], [1, 2, None], "heaviest first")
        self.assertEqual(by[1]["total_tokens"], 1000)
        self.assertEqual(by[1]["requests"], 2)
        self.assertEqual(by[1]["top_model"], "big")
        self.assertEqual(by[1]["cloud_tokens"], 900)
        self.assertEqual(by[None]["total_tokens"], 5)

    def test_a_run_gives_its_requests_their_user(self):
        self.db.execute("INSERT INTO route_runs VALUES ('r1', ?, 7)", (time.time(),))
        self._row(None, "m", 40, run="r1")                     # logged without a user, but the run knows it
        rows = usage.db_report_users(7)
        self.assertEqual([(r["user_id"], r["runs"]) for r in rows], [(7, 1)])

    def test_date_range_and_window_exclude_old_rows(self):
        self._row(1, "m", 10, ts=time.time() - 40 * 86400)
        self._row(1, "m", 20)
        self.assertEqual(usage.db_report_users(7)[0]["total_tokens"], 20)
        self.assertEqual(usage.db_report_users(365)[0]["total_tokens"], 30)

    def test_each_user_row_carries_its_own_per_model_usage(self):
        self._row(1, "small", 100)
        self._row(1, "small", 50)
        self._row(1, "big", 900, source="cloud")
        self._row(2, "small", 7)
        rows = {r["user_id"]: r for r in usage.db_report_users(7)}
        m1 = rows[1]["models"]
        self.assertEqual([(m["model"], m["source"], m["requests"], m["total_tokens"]) for m in m1],
                         [("big", "cloud", 1, 900), ("small", "local", 2, 150)], "heaviest model first")
        self.assertEqual([m["model"] for m in rows[2]["models"]], ["small"])
        self.assertEqual(sum(m["total_tokens"] for m in m1), rows[1]["total_tokens"])

    def test_models_follow_the_date_range(self):
        self._row(1, "old", 500, ts=time.time() - 40 * 86400)
        self._row(1, "new", 5)
        today = time.strftime("%Y-%m-%d")
        rows = usage.db_report_users(365, today, today)
        self.assertEqual([m["model"] for m in rows[0]["models"]], ["new"])

    def test_record_request_defaults_to_the_current_user(self):
        set_current_user(9)
        usage.db_record_request("chat/run", "m", 5, 5, 1.0, 1.0, None, True, 200)
        usage.db_record_request("chat/run", "m", 5, 5, 1.0, 1.0, None, True, 200, user_id=4)
        got = sorted(r[0] for r in self.db.execute("SELECT user_id FROM requests"))
        self.assertEqual(got, [4, 9])


if __name__ == "__main__":
    unittest.main()
