"""Loaded first by `unittest discover` (the name sorts first): from here on, the test run never writes into the real usage.db.

Before this, tests that drive the chat route or the lanes added rows to the real usage table on every run (a fake model called
"cloud-x" had 1,266 requests on the dashboard, and 'main' lane/verify rows with 0 tokens). Tests that patch their own database
object still write to it: only the real file is protected.
"""
import os
import unittest
from unittest import mock

os.environ["A770_NO_USAGE_WRITES"] = "1"


class UsageIsolation(unittest.TestCase):
    def test_the_real_usage_db_is_not_written_during_tests(self):
        from core.db import usage
        before = usage._usage_db.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        usage.db_record_request("test/endpoint", "isolation-check-model", 10, 5, 1.0, 0.1, None, False, 200)
        after = usage._usage_db.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        self.assertEqual(before, after)

    def test_a_test_with_its_own_database_still_records(self):
        import tempfile
        from pathlib import Path
        from core.db import usage
        from core.sqlite_util import ThreadLocalDB
        with tempfile.TemporaryDirectory() as d:
            db = ThreadLocalDB(Path(d) / "u.db")
            db.execute("""CREATE TABLE requests (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, endpoint TEXT, model TEXT, prompt_tokens INTEGER,
                completion_tokens INTEGER, total_tokens INTEGER, tps REAL, duration_s REAL, prompt_tps REAL, stream INTEGER, status INTEGER,
                prompt_cached_tokens INTEGER, completion_cached_tokens INTEGER, is_orchestrator INTEGER, source TEXT, provider TEXT,
                run_id TEXT, user_id INTEGER)""")
            with mock.patch.object(usage, "_usage_db", db):
                usage.db_record_request("test/endpoint", "m", 10, 5, 1.0, 0.1, None, False, 200)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 1)
            db.close() if hasattr(db, "close") else None


if __name__ == "__main__":
    unittest.main()
