"""tests/test_session_paging.py - Recent Chats are paged newest-first with a keyset cursor.

Run: python -m unittest tests.test_session_paging -v
"""

import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.db import sessions


def _db():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER, title TEXT, "
              "created_at REAL, user_id INTEGER)")
    for i in range(40):
        c.execute("INSERT INTO sessions (project_id, title, created_at, user_id) VALUES (NULL, ?, ?, 1)", (f"chat {i}", i))
    c.execute("INSERT INTO sessions (project_id, title, created_at, user_id) VALUES (NULL, 'other user', 0, 2)")
    c.execute("INSERT INTO sessions (project_id, title, created_at, user_id) VALUES (7, 'in project', 0, 1)")
    c.commit()
    return c


class SessionPaging(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(sessions, "_projects_db", _db())
        p.start()
        self.addCleanup(p.stop)

    def test_first_page_is_newest_and_reports_more(self):
        rows, more = sessions.db_list_sessions_page(0, 1, limit=15)
        self.assertEqual(len(rows), 15)
        self.assertTrue(more)
        self.assertEqual(rows[0]["title"], "chat 39")
        self.assertEqual([r["id"] for r in rows], sorted((r["id"] for r in rows), reverse=True))

    def test_pages_chain_without_overlap_until_the_end(self):
        seen, before, pages = [], None, 0
        while True:
            rows, more = sessions.db_list_sessions_page(0, 1, limit=15, before_id=before)
            seen += [r["id"] for r in rows]
            pages += 1
            if not more:
                break
            before = rows[-1]["id"]
        self.assertEqual(pages, 3)                      # 15 + 15 + 10
        self.assertEqual(len(seen), 40)
        self.assertEqual(len(set(seen)), 40)

    def test_exact_multiple_has_no_phantom_next_page(self):
        rows, more = sessions.db_list_sessions_page(0, 1, limit=40)
        self.assertEqual(len(rows), 40)
        self.assertFalse(more)

    def test_only_own_chat_sessions(self):
        titles = {r["title"] for r in sessions.db_list_sessions_page(0, 1)[0]}
        self.assertNotIn("other user", titles)
        self.assertNotIn("in project", titles)

    def test_no_limit_is_the_old_behaviour(self):
        rows, more = sessions.db_list_sessions_page(0, 1)
        self.assertEqual(len(rows), 40)
        self.assertFalse(more)
        self.assertEqual(len(sessions.db_list_sessions(0, 1)), 40)

    def test_project_pages_check_ownership(self):
        with mock.patch.object(sessions, "db_project_owner", lambda pid: 1):
            rows, more = sessions.db_list_sessions_page(7, 1, limit=15)
            self.assertEqual([r["title"] for r in rows], ["in project"])
            self.assertFalse(more)
        with mock.patch.object(sessions, "db_project_owner", lambda pid: 2):
            with self.assertRaises(PermissionError):
                sessions.db_list_sessions_page(7, 1, limit=15)


if __name__ == "__main__":
    unittest.main()
