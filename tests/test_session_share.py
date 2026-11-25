"""tests/test_session_share.py - secure public share URL for sessions."""

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.db import (
    db_create_or_get_share,
    db_get_shared_session,
    db_revoke_share,
)
from core.db import common as db_common


def _setup_test_db():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER,
            title TEXT, created_at REAL, user_id INTEGER, agent_id INTEGER
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            role TEXT NOT NULL, content TEXT NOT NULL, meta TEXT, created_at REAL NOT NULL
        );
        CREATE TABLE shared_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL UNIQUE REFERENCES sessions(id) ON DELETE CASCADE,
            share_key TEXT UNIQUE NOT NULL,
            created_at REAL NOT NULL,
            user_id INTEGER
        );
        CREATE INDEX idx_shared_sessions_key ON shared_sessions(share_key);
    """)
    c.commit()
    return c


class TestSessionShare(unittest.TestCase):
    def setUp(self):
        self.db = _setup_test_db()
        self.patcher = mock.patch.object(db_common, "_projects_db", self.db)
        self.patcher2 = mock.patch("core.db.projects._get_projects_db", lambda: self.db)
        self.patcher3 = mock.patch("core.db.sessions._projects_db", self.db)
        self.patcher4 = mock.patch("core.db.sessions.transaction", lambda db: mock.MagicMock(__enter__=lambda s: None, __exit__=lambda s, *a: None))
        self.patcher.start()
        self.patcher2.start()
        self.patcher3.start()
        self.patcher4.start()
        # Seed session owned by user 10
        self.db.execute("INSERT INTO sessions (id, title, created_at, user_id) VALUES (1, 'BFSI Analysis', 1000.0, 10)")
        self.db.execute(
            "INSERT INTO messages (session_id, role, content, meta, created_at) VALUES "
            "(1, 'user', 'Analyze BFSI performance', NULL, 1001.0)"
        )
        self.db.execute(
            "INSERT INTO messages (session_id, role, content, meta, created_at) VALUES "
            "(1, 'assistant', 'Here is the report:\n```mermaid\ngraph TD\nA-->B\n```', '{\"tool_calls\": [\"secret_bash\"], \"reasoning\": \"thinking\"}', 1002.0)"
        )
        self.db.commit()

    def tearDown(self):
        self.patcher.stop()
        self.patcher2.stop()
        self.patcher3.stop()
        self.patcher4.stop()

    def test_share_creates_cryptographic_key_and_reuses_it(self):
        k1 = db_create_or_get_share(1, 10)
        self.assertTrue(len(k1) >= 20)
        k2 = db_create_or_get_share(1, 10)
        self.assertEqual(k1, k2)

    def test_non_owner_cannot_share(self):
        with self.assertRaises(PermissionError):
            db_create_or_get_share(1, 999)

    def test_get_shared_session_returns_clean_content_without_tool_calls(self):
        key = db_create_or_get_share(1, 10)
        data = db_get_shared_session(key)
        self.assertIsNotNone(data)
        self.assertEqual(data["title"], "BFSI Analysis")
        self.assertEqual(len(data["messages"]), 2)
        # Verify tool calls are NOT leaked
        assistant_msg = data["messages"][1]
        self.assertEqual(assistant_msg["role"], "assistant")
        self.assertIn("Here is the report", assistant_msg["content"])
        self.assertNotIn("tool_calls", assistant_msg)

    def test_revoked_share_returns_none(self):
        key = db_create_or_get_share(1, 10)
        revoked = db_revoke_share(1, 10)
        self.assertTrue(revoked)
        self.assertIsNone(db_get_shared_session(key))

    def test_invalid_key_returns_none(self):
        self.assertIsNone(db_get_shared_session("non_existent_key"))


class TestShareEndpoints(unittest.TestCase):
    def setUp(self):
        self.db = _setup_test_db()
        self.patcher = mock.patch.object(db_common, "_projects_db", self.db)
        self.patcher2 = mock.patch("core.db.projects._get_projects_db", lambda: self.db)
        self.patcher3 = mock.patch("core.db.sessions._projects_db", self.db)
        self.patcher4 = mock.patch("core.db.sessions.transaction", lambda db: mock.MagicMock(__enter__=lambda s: None, __exit__=lambda s, *a: None))
        self.patcher.start()
        self.patcher2.start()
        self.patcher3.start()
        self.patcher4.start()

        self.db.execute("INSERT INTO sessions (id, title, created_at, user_id) VALUES (1, 'BFSI Analysis', 1000.0, 10)")
        self.db.execute(
            "INSERT INTO messages (session_id, role, content, meta, created_at) VALUES "
            "(1, 'user', 'Analyze BFSI performance', NULL, 1001.0)"
        )
        self.db.execute(
            "INSERT INTO messages (session_id, role, content, meta, created_at) VALUES "
            "(1, 'assistant', 'Here is the report', '{\"tool_calls\": [\"tool_1\"]}', 1002.0)"
        )
        self.db.commit()

        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routes.pages import router as pages_router
        from routes.projects.session_endpoints import router as session_router
        from core.deps import get_current_user
        from core.auth import Principal

        self.app = FastAPI()
        self.app.include_router(pages_router)
        self.app.include_router(session_router)

        # Mock authenticated user as user 10 (owner)
        self.app.dependency_overrides[get_current_user] = lambda: Principal(
            id=10, username="tester", display_name="Tester", is_super_admin=False, must_change_password=False,
            role_names=["user"], permission_keys={"chat.use"}
        )
        self.client = TestClient(self.app)

    def tearDown(self):
        self.patcher.stop()
        self.patcher2.stop()
        self.patcher3.stop()
        self.patcher4.stop()

    def test_post_share_returns_share_url(self):
        resp = self.client.post("/control/sessions/1/share")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("ok"))
        self.assertTrue(data.get("share_key"))
        self.assertTrue(data.get("share_url").startswith("/share/"))

    def test_public_share_endpoint_requires_no_auth(self):
        # 1. Share the session
        s_resp = self.client.post("/control/sessions/1/share")
        key = s_resp.json()["share_key"]

        # 2. Call public endpoint using an unauthenticated client
        from fastapi.testclient import TestClient
        from routes.pages import router as pages_router
        from fastapi import FastAPI

        pub_app = FastAPI()
        pub_app.include_router(pages_router)
        pub_client = TestClient(pub_app)

        res = pub_client.get(f"/public/share/{key}")
        self.assertEqual(res.status_code, 200)
        payload = res.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["title"], "BFSI Analysis")
        self.assertEqual(len(payload["messages"]), 2)
        # Check messages
        self.assertEqual(payload["messages"][0]["role"], "user")
        self.assertEqual(payload["messages"][1]["role"], "assistant")
        self.assertNotIn("tool_calls", payload["messages"][1])
