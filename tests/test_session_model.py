"""Each chat keeps its own model (sessions.model): stored at creation, changed by its owner only, listed with the chat."""
import sqlite3
import unittest
from unittest import mock

from core.db import sessions


def _db():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER, title TEXT, "
              "created_at REAL, user_id INTEGER, agent_id INTEGER, model TEXT)")
    return c


class SessionModel(unittest.TestCase):
    def setUp(self):
        db = _db()
        for p in (mock.patch.object(sessions, "_projects_db", db),
                  mock.patch.object(sessions, "db_session_owner", lambda sid: {r["id"]: r["user_id"] for r in db.execute("SELECT id, user_id FROM sessions")}.get(sid)),
                  mock.patch.object(sessions, "_at_rest", lambda t: t)):
            p.start()
            self.addCleanup(p.stop)

    def test_created_with_a_model_and_listed(self):
        s = sessions.db_create_session(0, "a", owner_user_id=1, model="cloud-x")
        self.assertEqual(s["model"], "cloud-x")
        self.assertEqual(sessions.db_list_sessions_page(0, 1)[0][0]["model"], "cloud-x")

    def test_two_chats_keep_different_models(self):
        a = sessions.db_create_session(0, "a", owner_user_id=1, model="m1")["id"]
        b = sessions.db_create_session(0, "b", owner_user_id=1, model="m2")["id"]
        sessions.db_update_session_model(a, "m3", owner_user_id=1)
        by_id = {r["id"]: r["model"] for r in sessions.db_list_sessions_page(0, 1)[0]}
        self.assertEqual(by_id, {a: "m3", b: "m2"})

    def test_only_the_owner_can_change_it(self):
        a = sessions.db_create_session(0, "a", owner_user_id=1, model="m1")["id"]
        with self.assertRaises(PermissionError):
            sessions.db_update_session_model(a, "evil", owner_user_id=2)

    def test_empty_means_no_model(self):
        a = sessions.db_create_session(0, "a", owner_user_id=1, model="m1")["id"]
        sessions.db_update_session_model(a, "  ", owner_user_id=1)
        self.assertIsNone(sessions.db_list_sessions_page(0, 1)[0][0]["model"])


if __name__ == "__main__":
    unittest.main()
