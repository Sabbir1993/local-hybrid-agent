"""tests/test_login_lockout.py - PCI DSS 8.3.4 account lockout.

Uses an in-memory auth DB (never touches auth.db).
Run: python -m unittest tests.test_login_lockout -v
"""

import sqlite3
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import auth_db, audit
from core.auth_provider import LocalAuthProvider, hash_password


class LockoutTests(unittest.TestCase):
    def setUp(self):
        self._saved_db = auth_db._auth_db
        self._saved_audit = audit.audit_log
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("""CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, display_name TEXT,
            password_hash TEXT, auth_provider TEXT, is_active INTEGER, is_super_admin INTEGER,
            must_change_password INTEGER, failed_login_count INTEGER DEFAULT 0, locked_until REAL,
            last_login_at REAL)""")
        conn.execute("INSERT INTO users VALUES (1, 'alice', 'Alice', ?, 'local', 1, 0, 0, 0, NULL, NULL)",
                     (hash_password("[PLACEHOLDER]-correct"),))
        conn.commit()
        auth_db._auth_db = conn
        audit.audit_log = lambda *a, **k: None
        self.p = LocalAuthProvider()

    def tearDown(self):
        auth_db._auth_db = self._saved_db
        audit.audit_log = self._saved_audit

    def test_locks_after_max_failures_even_with_correct_password(self):
        for _ in range(auth_db.MAX_FAILED_LOGINS):
            self.assertIsNone(self.p.verify_credentials("alice", "wrong"))
        self.assertTrue(auth_db.is_locked(auth_db.get_user_by_username("alice")))
        self.assertIsNone(self.p.verify_credentials("alice", "[PLACEHOLDER]-correct"))

    def test_success_before_limit_resets_counter(self):
        for _ in range(auth_db.MAX_FAILED_LOGINS - 1):
            self.p.verify_credentials("alice", "wrong")
        self.assertIsNotNone(self.p.verify_credentials("alice", "[PLACEHOLDER]-correct"))
        self.assertEqual(auth_db.get_user_by_username("alice")["failed_login_count"], 0)

    def test_lock_expires_and_admin_unlock(self):
        for _ in range(auth_db.MAX_FAILED_LOGINS):
            self.p.verify_credentials("alice", "wrong")
        auth_db.unlock_user(1)
        self.assertIsNotNone(self.p.verify_credentials("alice", "[PLACEHOLDER]-correct"))
        auth_db.db().execute("UPDATE users SET locked_until = ? WHERE id = 1", (time.time() - 1,))
        self.assertFalse(auth_db.is_locked(auth_db.get_user_by_username("alice")))


if __name__ == "__main__":
    unittest.main()
