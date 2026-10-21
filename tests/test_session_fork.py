"""tests/test_session_fork.py - P2 session fork/branch: the movable head.

A "rewind" that destroys the original history can lose work the user asked the
old branch to keep; a fork copies instead and the original stays untouched, so
retrying from an earlier point is strictly additive. The whole feature is
DEFAULT-OFF (`sessions.fork_enabled`): the endpoint answers 403 until it is
armed in config - nothing user-visible changes at merge.

Run: python -m unittest tests.test_session_fork -v
"""

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.db.common import _init_projects_db


def _db(with_fork_of=True):
    """The tables a fork touches, in one in-memory projects-db shaped like the real one."""
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.executescript(f"""
        CREATE TABLE sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER,
            title TEXT, created_at REAL, user_id INTEGER, agent_id INTEGER{", fork_of INTEGER" if with_fork_of else ""});
        CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            role TEXT NOT NULL, content TEXT NOT NULL, meta TEXT, created_at REAL NOT NULL);
        CREATE TABLE plan_items (id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL, ord INTEGER NOT NULL, text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', note TEXT, deps TEXT DEFAULT '[]',
            phase TEXT, verification TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE session_working_memory (session_id INTEGER PRIMARY KEY,
            user_id INTEGER, content TEXT NOT NULL, updated_at REAL NOT NULL);
    """)
    c.commit()
    return c


def _seed(c, n_messages=6, with_plan=True):
    """session 1 owned by user 7 + n user/assistant pairs; the others belong to user 2."""
    c.execute("INSERT INTO sessions (project_id, title, created_at, user_id) VALUES (NULL, 'my chat', 1.0, 7)")
    roles = ["user", "assistant"]
    for i in range(n_messages):
        c.execute("INSERT INTO messages (session_id, role, content, created_at) VALUES (1, ?, ?, ?)",
                  (roles[i % 2], f"m{i}", 2.0 + i))
    c.execute("INSERT INTO sessions (project_id, title, created_at, user_id) VALUES (NULL, 'other', 0.9, 2)")
    if with_plan:
        c.execute("INSERT INTO plan_items (session_id, ord, text, status, created_at, updated_at) "
                  "VALUES (1, 1, 'step one', 'done', 3.0, 4.0)")
        c.execute("INSERT INTO plan_items (session_id, ord, text, status, created_at, updated_at) "
                  "VALUES (1, 2, 'step two', 'in_progress', 3.0, 4.5)")
        c.execute("INSERT INTO session_working_memory (session_id, user_id, content, updated_at) "
                  "VALUES (1, 7, 'goal/files/plan digest', 5.0)")
    c.commit()
    return c


class SessionFork(unittest.TestCase):
    def setUp(self):
        import core.db as db_pkg
        from core.db import projects as projects_mod
        from core.db import sessions
        self.sessions = sessions
        self.c = _seed(_db())
        p1 = mock.patch.object(sessions, "_projects_db", self.c)
        p2 = mock.patch.object(projects_mod, "_projects_db", self.c)
        p3 = mock.patch.object(db_pkg, "_projects_db", self.c)    # db_session_owner resolves through this one
        p1.start(); p2.start(); p3.start()
        self.addCleanup(p1.stop); self.addCleanup(p2.stop); self.addCleanup(p3.stop)

    def test_fork_copies_only_messages_up_to_and_patches_fork_of(self):
        out = self.sessions.db_fork_session(1, owner_user_id=7, upto_message_id=3)
        self.assertNotEqual(out["id"], 1)
        src = [dict(r) for r in self.c.execute("SELECT * FROM messages WHERE session_id = 1")]
        fork = [dict(r) for r in self.c.execute("SELECT * FROM messages WHERE session_id = ?", (out["id"],))]
        self.assertEqual([m["content"] for m in fork], ["m0", "m1", "m2"])   # kept the head, up to AND incl. m2
        self.assertEqual([m["content"] for m in src], ["m0", "m1", "m2", "m3", "m4", "m5"])   # source untouched
        row = self.c.execute("SELECT fork_of, title FROM sessions WHERE id = ?", (out["id"],)).fetchone()
        self.assertEqual(row["fork_of"], 1)
        self.assertIn("my chat", row["title"])

    def test_fork_without_upto_copies_everything(self):
        out = self.sessions.db_fork_session(1, owner_user_id=7)
        fork = [r["content"] for r in self.c.execute(
            "SELECT content FROM messages WHERE session_id = ?", (out["id"],))]
        self.assertEqual(len(fork), 6)

    def test_fork_carries_plan_state_and_working_memory(self):
        out = self.sessions.db_fork_session(1, owner_user_id=7, upto_message_id=3)
        sid = out["id"]
        plan = [(r["ord"], r["status"]) for r in self.c.execute(
            "SELECT ord, status FROM plan_items WHERE session_id = ? ORDER BY ord", (sid,))]
        self.assertEqual(plan, [(1, "done"), (2, "in_progress")])     # statuses copied: a retry resumes the same plan
        wm = self.c.execute("SELECT content FROM session_working_memory WHERE session_id = ?", (sid,)).fetchone()
        self.assertIsNotNone(wm)
        self.assertEqual(wm["content"], "goal/files/plan digest")

    def test_fork_counts_copied_messages(self):
        out = self.sessions.db_fork_session(1, owner_user_id=7, upto_message_id=4)
        self.assertEqual(out["messages"], 4)      # ids 1..4 exist in this session

    def test_not_owner_is_refused(self):
        with self.assertRaises(PermissionError):
            self.sessions.db_fork_session(1, owner_user_id=2)          # session 1 belongs to user 7
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 2)

    def test_unknown_message_is_refused_loudly(self):
        with self.assertRaises(ValueError):
            self.sessions.db_fork_session(1, owner_user_id=7, upto_message_id=999)


class LegacyMigration(unittest.TestCase):
    def test_sessions_gain_fork_of_on_legacy_db(self):
        with tempfile.TemporaryDirectory() as td:
            db_file = Path(td) / "projects.db"
            legacy = sqlite3.connect(db_file)
            legacy.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER, "
                           "title TEXT, created_at REAL, user_id INTEGER, agent_id INTEGER)")   # no fork_of
            legacy.execute("INSERT INTO sessions (project_id, title, created_at, user_id) VALUES "
                           "(NULL, 'old chat', 0.0, 1)")
            legacy.commit()
            legacy.close()
            import core.db as db_mod
            import core.db.common as common
            with mock.patch.object(common, "PROJECTS_DB_FILE", str(db_file)), \
                 mock.patch.object(db_mod, "PROJECTS_DB_FILE", str(db_file)):
                conn = _init_projects_db()          # IF NOT EXISTS keeps the legacy shape, then migrates
                try:
                    cols = [r[1] for r in conn.execute("PRAGMA table_info(sessions)")]
                    self.assertIn("fork_of", cols)
                    kept = conn.execute("SELECT title FROM sessions").fetchall()
                    self.assertEqual([r["title"] for r in kept], ["old chat"])
                finally:
                    conn.close()


class EndpointWiring(unittest.TestCase):
    src = "\n".join(p.read_text(encoding="utf-8")
                    for p in (Path("routes/projects/session_endpoints.py"), Path("routes/projects/models.py")))

    def test_fork_endpoint_is_flag_gated(self):
        self.assertIn("@router.post(\"/control/sessions/{sid}/fork\")", self.src)
        self.assertIn("sessions.fork_enabled", self.src)               # disabled state is visible in the gate
        self.assertIn("fork_enabled", self.src)
        self.assertIn("403", self.src)                                 # default-off answer

    def test_endpoint_uses_the_db_function(self):
        self.assertIn("db_fork_session(", self.src)
        self.assertIn("upto_message_id", self.src)


if __name__ == "__main__":
    unittest.main()
