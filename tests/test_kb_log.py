"""KB retrieval decision log: reason codes and counts only."""
import sqlite3
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import kb_log  # noqa: E402


class KbLogTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self._p = mock.patch.object(kb_log, "_usage_db", self.db)
        self._p.start()
        kb_log._init()

    def tearDown(self):
        self._p.stop()

    def test_records_and_aggregates_by_reason(self):
        kb_log.record(1, "chat", False, "attachment_analysis")
        kb_log.record(1, "chat", False, "attachment_analysis")
        kb_log.record(2, "chat", True, "company_intent", hits=6, top_cos=0.71, injected=True)
        kb_log.record(2, "agent", True, "company_intent", hits=2, top_cos=0.4, injected=False)
        st = kb_log.stats(7)
        self.assertEqual(st["total"], 4)
        by = {(b["reason"], b["consulted"]): b for b in st["by_reason"]}
        self.assertEqual(by[("attachment_analysis", False)]["count"], 2)
        ci = by[("company_intent", True)]
        self.assertEqual((ci["count"], ci["injected"]), (2, 1))
        self.assertAlmostEqual(ci["avg_top_cos"], 0.555, places=3)

    def test_no_text_is_stored_and_old_rows_are_purged(self):
        cols = [r[1] for r in self.db.execute("PRAGMA table_info(kb_decisions)")]
        self.assertFalse({"query", "text", "message"} & set(cols), cols)
        self.db.execute("INSERT INTO kb_decisions (ts, reason, consulted) VALUES (?, 'old', 1)", (time.time() - 200 * 86400,))
        kb_log._last_purge = 0.0
        kb_log.record(1, "chat", True, "company_intent")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM kb_decisions WHERE reason='old'").fetchone()[0], 0)

    def test_a_broken_db_never_breaks_a_chat(self):
        with mock.patch.object(kb_log, "_usage_db", None):
            kb_log.record(1, "chat", True, "x")             # swallowed
            self.assertEqual(kb_log.stats(1)["total"], 0)


if __name__ == "__main__":
    unittest.main()
