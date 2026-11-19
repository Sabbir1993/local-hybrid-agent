"""Sidebar tree (db_session_groups): per agent / project, newest activity first, a total, and only the caller's rows."""
import sqlite3
import unittest
from unittest import mock

from core.db import sessions


def _db():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER, title TEXT, "
              "created_at REAL, user_id INTEGER, agent_id INTEGER, model TEXT)")
    c.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER, role TEXT, "
              "content TEXT, meta TEXT, created_at REAL)")
    return c


class SessionGroups(unittest.TestCase):
    def setUp(self):
        self.db = _db()
        p = mock.patch.object(sessions, "_projects_db", self.db)
        p.start()
        self.addCleanup(p.stop)

    def _s(self, title, created, user=1, agent=None, project=None):
        cur = self.db.execute("INSERT INTO sessions (project_id, title, created_at, user_id, agent_id) VALUES (?,?,?,?,?)",
                              (project, title, created, user, agent))
        return cur.lastrowid

    def _msg(self, sid, at):
        self.db.execute("INSERT INTO messages (session_id, role, content, created_at) VALUES (?,?,?,?)", (sid, "user", "x", at))

    def test_agent_groups_sorted_by_last_activity(self):
        old = self._s("old", 100, agent=7)
        new = self._s("new", 200, agent=7)
        self._msg(old, 500)                       # the older chat was used last
        g = sessions.db_session_groups(1, "agent", ["7"], 5)["7"]
        self.assertEqual(g["total"], 2)
        self.assertEqual([s["id"] for s in g["sessions"]], [old, new])
        self.assertEqual(g["sessions"][0]["last_at"], 500)

    def test_per_group_limit_keeps_total(self):
        for i in range(7):
            self._s(f"s{i}", i, agent=3)
        g = sessions.db_session_groups(1, "agent", [3], 5)["3"]
        self.assertEqual((g["total"], len(g["sessions"])), (7, 5))

    def test_only_the_callers_rows(self):
        self._s("mine", 1, agent=4)
        self._s("theirs", 2, user=2, agent=4)
        g = sessions.db_session_groups(1, "agent", ["4"], 5)["4"]
        self.assertEqual([s["title"] for s in g["sessions"]], ["mine"])

    def test_agent_kind_skips_project_tasks_and_project_kind_groups_by_project(self):
        self._s("agent chat", 1, agent=5)
        self._s("project task", 2, agent=5, project=9)
        self.assertEqual(sessions.db_session_groups(1, "agent", [5], 5)["5"]["total"], 1)
        self.assertEqual(sessions.db_session_groups(1, "project", [9], 5)["9"]["sessions"][0]["title"], "project task")

    def test_empty_and_bad_ids(self):
        self.assertEqual(sessions.db_session_groups(1, "agent", [], 5), {})
        self.assertEqual(sessions.db_session_groups(1, "agent", ["x", "8"], 5), {"8": {"total": 0, "sessions": []}})


if __name__ == "__main__":
    unittest.main()
