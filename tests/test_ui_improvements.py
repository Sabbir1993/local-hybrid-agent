"""Tests for recent UI improvements: session pinning, search, and user profile updates."""
import sqlite3
import unittest
from unittest import mock

from core.db import sessions
from core import auth_db


def _db():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER, title TEXT, "
              "created_at REAL, user_id INTEGER, agent_id INTEGER, model TEXT, is_pinned INTEGER DEFAULT 0)")
    c.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER, role TEXT, "
              "content TEXT, meta TEXT, created_at REAL)")
    return c


class TestUIImprovements(unittest.TestCase):
    def setUp(self):
        self.db = _db()
        p1 = mock.patch.object(sessions, "_projects_db", self.db)
        p1.start()
        self.addCleanup(p1.stop)

        p2 = mock.patch("core.db.projects._get_projects_db", return_value=self.db)
        p2.start()
        self.addCleanup(p2.stop)

    def test_pin_and_search_sessions(self):
        # Create sessions
        cur = self.db.execute("INSERT INTO sessions (title, created_at, user_id, is_pinned) VALUES (?, ?, ?, ?)",
                              ("QR Code Generator", 100, 1, 0))
        s1 = cur.lastrowid

        cur = self.db.execute("INSERT INTO sessions (title, created_at, user_id, is_pinned) VALUES (?, ?, ?, ?)",
                              ("Payment API Service", 200, 1, 0))
        s2 = cur.lastrowid

        # Insert messages
        self.db.execute("INSERT INTO messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                        (s2, "user", "How do I create a QR code payment flow?", 210))

        # Test search by title
        res = sessions.db_search_sessions(1, "QR")
        self.assertEqual(len(res), 2)  # s1 has "QR" in title, s2 has "QR" in message content

        # Test pin
        sessions.db_update_session_pinned(s1, True, 1)
        row = self.db.execute("SELECT is_pinned FROM sessions WHERE id = ?", (s1,)).fetchone()
        self.assertEqual(row["is_pinned"], 1)

        # Search ordered by is_pinned desc
        res2 = sessions.db_search_sessions(1, "QR")
        self.assertEqual(res2[0]["id"], s1)
        self.assertTrue(res2[0]["is_pinned"])

    def test_user_profile_update(self):
        # Verify update_user_profile works on in-memory auth db
        auth_conn = sqlite3.connect(":memory:", check_same_thread=False)
        auth_conn.row_factory = sqlite3.Row
        auth_conn.execute("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE,
                password_hash TEXT,
                display_name TEXT,
                email TEXT,
                image TEXT,
                employee_id TEXT,
                department TEXT,
                designation TEXT,
                dob TEXT,
                gender TEXT
            )
        """)
        auth_conn.execute("INSERT INTO users (id, username, display_name) VALUES (1, 'alice', 'Alice')")
        auth_conn.commit()

        with mock.patch("core.auth_db.users.db", return_value=auth_conn):
            updated = auth_db.users.update_user_profile(1, {
                "display_name": "Alice Wonderland",
                "email": "alice@example.com",
                "department": "Engineering",
                "designation": "Lead Architect",
            })
            self.assertEqual(updated["display_name"], "Alice Wonderland")
            self.assertEqual(updated["email"], "alice@example.com")
            self.assertEqual(updated["department"], "Engineering")
            self.assertEqual(updated["designation"], "Lead Architect")
