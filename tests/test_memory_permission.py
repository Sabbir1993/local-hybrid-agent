"""tests/test_memory_permission.py - the memory.use permission.

It decides whether a person may use their personal memory (Settings -> Capabilities -> My memory): the
five /agent/memory endpoints, the memory block in chat and in agent mode, and the memory tools. The seeding
tests cover the "granted once to roles that hold chat.use" rule, so an admin's later revoke survives restarts.
Memory text in these tests is placeholder text only.

Run: python -m unittest tests.test_memory_permission -v
"""

import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import agent_memory, auth_db, deps
from core.agent_tools import memory_tools as mt
from core.auth import Principal
from core.request_context import set_current_user
from routes.agent import memory_endpoints

MEM_TEXT = "PLACEHOLDER-MEMORY-NOTE"


def _perm_keys(role):
    return {r["key"] for r in auth_db._auth_db.execute(
        "SELECT p.key FROM role_permissions rp JOIN roles r ON r.id = rp.role_id "
        "JOIN permissions p ON p.id = rp.permission_id WHERE r.name = ?", (role,))}


class _TempAuthDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = auth_db._auth_db
        with mock.patch.object(auth_db, "AUTH_DB_FILE", Path(self.tmp.name) / "auth.db"):
            auth_db._auth_db = auth_db._init_auth_db()
        self.db = auth_db._auth_db

    def tearDown(self):
        set_current_user(None)
        self.db.close()
        auth_db._auth_db = self._saved
        self.tmp.cleanup()

    def add_user(self, uid, role, is_super=0):
        now = time.time()
        self.db.execute("INSERT INTO users (id, username, is_super_admin, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                        (uid, f"user{uid}", is_super, now, now))
        self.db.execute("INSERT INTO user_roles (user_id, role_id, assigned_at) "
                        "SELECT ?, id, ? FROM roles WHERE name = ?", (uid, now, role))
        self.db.commit()

    def revoke(self, role, key):
        self.db.execute("DELETE FROM role_permissions WHERE role_id = (SELECT id FROM roles WHERE name = ?) "
                        "AND permission_id = (SELECT id FROM permissions WHERE key = ?)", (role, key))
        self.db.commit()


class SeedTests(_TempAuthDb):
    def test_fresh_install_gives_user_and_admin_the_key(self):
        self.assertIn("memory.use", _perm_keys("user"))
        self.assertIn("memory.use", _perm_keys("admin"))

    def test_revoke_from_user_survives_a_restart(self):
        self.revoke("user", "memory.use")
        auth_db.init_tables(self.db)
        auth_db.init_tables(self.db)
        self.assertNotIn("memory.use", _perm_keys("user"))
        self.assertIn("chat.use", _perm_keys("user"))
        self.assertIn("memory.use", _perm_keys("admin"))

    def test_upgrade_grants_once_to_every_role_with_chat_use(self):
        now = time.time()
        self.db.execute("INSERT INTO roles (name, description, is_builtin, created_at) VALUES ('team_a', '', 0, ?)", (now,))
        self.db.execute("INSERT INTO roles (name, description, is_builtin, created_at) VALUES ('no_chat', '', 0, ?)", (now,))
        self.db.execute("INSERT INTO role_permissions (role_id, permission_id, granted_at) "
                        "SELECT r.id, p.id, ? FROM roles r, permissions p WHERE r.name = 'team_a' AND p.key = 'chat.use'", (now,))
        self.db.execute("INSERT INTO role_permissions (role_id, permission_id, granted_at) "
                        "SELECT r.id, p.id, ? FROM roles r, permissions p WHERE r.name = 'no_chat' AND p.key = 'audit.view'", (now,))
        # an install from before the key existed: no key row, no grants
        self.db.execute("DELETE FROM role_permissions WHERE permission_id IN (SELECT id FROM permissions WHERE key = 'memory.use')")
        self.db.execute("DELETE FROM permissions WHERE key = 'memory.use'")
        self.db.commit()
        auth_db.init_tables(self.db)
        self.assertIn("memory.use", _perm_keys("team_a"))
        self.assertIn("memory.use", _perm_keys("user"))
        self.assertNotIn("memory.use", _perm_keys("no_chat"))
        # the custom role's admin takes it away; the next start leaves it that way
        self.revoke("team_a", "memory.use")
        auth_db.init_tables(self.db)
        self.assertNotIn("memory.use", _perm_keys("team_a"))


def _principal(perms, is_super=False):
    return Principal(id=7, username="u7", display_name="u7", is_super_admin=is_super,
                     must_change_password=False, role_names=[], permission_keys=set(perms))


class EndpointTests(unittest.TestCase):
    perms = {"chat.use"}

    def client(self, perms):
        app = FastAPI()
        app.include_router(memory_endpoints.router)
        app.dependency_overrides[deps.get_current_user] = lambda: _principal(perms)
        c = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(c.close)
        return c

    def test_refused_without_the_key(self):
        c = self.client({"chat.use"})
        with mock.patch.object(deps, "audit_log"):
            for method, url in (("get", "/agent/memory"), ("get", "/agent/memory/a.md"), ("put", "/agent/memory/a.md"),
                                ("delete", "/agent/memory/a.md"), ("delete", "/agent/memory")):
                r = getattr(c, method)(url, **({"json": {"content": MEM_TEXT}} if method == "put" else {}))
                self.assertEqual(r.status_code, 403, (method, url))
                self.assertIn("memory.use", r.text)

    def test_allowed_with_the_key(self):
        c = self.client({"chat.use", "memory.use"})
        with mock.patch.object(agent_memory, "list_files", return_value=[]):
            r = c.get("/agent/memory")
        self.assertEqual(r.status_code, 200, r.text[:200])
        self.assertEqual(r.json()["files"], [])
        with mock.patch.object(agent_memory, "delete_all", return_value=0):
            self.assertEqual(c.delete("/agent/memory").status_code, 200)


class ToolTests(_TempAuthDb):
    def call(self, name, **args):
        return mt.MEMORY_IMPLS[name](args)

    def test_refused_without_the_key_and_works_with_it(self):
        self.add_user(1, "user")
        self.add_user(2, "user")
        self.revoke("user", "memory.use")
        set_current_user(1)
        out = self.call("memory_list")
        self.assertEqual(out, "error: personal memory is not enabled for your account")
        self.assertIn("not enabled", self.call("memory_append", path="p.md", line=MEM_TEXT, description="d"))
        self.assertEqual(agent_memory.list_files(1), [])
        # a custom role that was granted it by name works; so does a super admin
        now = time.time()
        self.db.execute("INSERT INTO roles (name, description, is_builtin, created_at) VALUES ('mem', '', 0, ?)", (now,))
        self.db.execute("INSERT INTO role_permissions (role_id, permission_id, granted_at) "
                        "SELECT r.id, p.id, ? FROM roles r, permissions p WHERE r.name = 'mem' AND p.key = 'memory.use'", (now,))
        self.db.execute("INSERT INTO user_roles (user_id, role_id, assigned_at) SELECT 2, id, ? FROM roles WHERE name = 'mem'", (now,))
        self.add_user(3, "user", is_super=1)
        self.db.commit()
        for uid in (2, 3):
            set_current_user(uid)
            self.assertIn("added to p.md", self.call("memory_append", path="p.md", line=MEM_TEXT, description="d"))
            self.assertIn("p.md", self.call("memory_list"))


class ChatTests(unittest.TestCase):
    """The real chat route with a fake model: the memory block is in the system message only with the key."""

    def turn(self, user):
        from routes import chat as chat_route
        from core.small_model import APP_CONFIG
        seen = {}

        async def fake_stream(client, msgs, tools=None, **kw):
            seen["msgs"] = [dict(m) for m in msgs]
            raise RuntimeError("stop after capturing the payload")
            yield  # pragma: no cover

        async def fake_fallback(client, fb, msgs, tools=None, *a, **kw):
            async for x in fake_stream(client, msgs, tools=tools):
                yield x

        lane = SimpleNamespace(model_id="cloud-x", display="cloud-x", provider_name="p")
        req = chat_route.ChatRunRequest(messages=[{"role": "user", "content": "hello there"}], web_search=False,
                                        kb="off", session_id=78)
        saved = APP_CONFIG.get("memory")
        APP_CONFIG["memory"] = {"enabled": True, "allow_cloud": True}

        async def main():
            run = chat_route.run
            with (mock.patch.object(run.cloud, "cloud_lane", return_value=lane), mock.patch.object(run.cloud, "CloudClient"),
                  mock.patch.object(run.input_guard, "check_async", mock.AsyncMock(return_value=None)),
                  mock.patch.object(run, "allowed_source_ids_for", return_value=set()),
                  mock.patch.object(agent_memory, "session_block", return_value=MEM_TEXT),
                  mock.patch.object(run.state, "is_running", return_value=True), mock.patch.object(run, "audit_log"),
                  mock.patch.object(run, "monitor_begin", return_value=1), mock.patch.object(run, "monitor_end", create=True),
                  mock.patch.object(run, "db_record_request"), mock.patch.object(run, "_llm_chat_stream", fake_stream),
                  mock.patch.object(run.common, "_llm_chat_stream_with_fallback", fake_fallback)):
                resp = await chat_route.chat_run(req, user)
                try:
                    n = 0
                    async for _ in resp.body_iterator:
                        n += 1
                        if n > 20:
                            break
                except Exception:
                    pass
        try:
            asyncio.run(main())
        finally:
            if saved is None:
                APP_CONFIG.pop("memory", None)
            else:
                APP_CONFIG["memory"] = saved
        return seen

    def _system(self, user):
        seen = self.turn(user)
        self.assertTrue(seen.get("msgs"), "the model was not called")
        return "\n".join(str(m["content"]) for m in seen["msgs"] if m["role"] == "system")

    def test_memory_block_needs_the_key(self):
        plain = SimpleNamespace(id=1, username="u1", is_super_admin=False, role_names=[], permission_keys={"chat.use"})
        self.assertNotIn(MEM_TEXT, self._system(plain))
        holder = SimpleNamespace(id=1, username="u1", is_super_admin=False, role_names=[],
                                 permission_keys={"chat.use", "memory.use"})
        self.assertIn(MEM_TEXT, self._system(holder))
        root = SimpleNamespace(id=1, username="u1", is_super_admin=True, role_names=[], permission_keys=set())
        self.assertIn(MEM_TEXT, self._system(root))


if __name__ == "__main__":
    unittest.main()
