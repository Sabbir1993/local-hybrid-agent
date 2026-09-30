"""
tests/test_custom_agents.py - Tests for user-wise custom agents (DB, roles, REST APIs,
and tool-allowlist enforcement).

Every test runs against a throwaway auth.db (never the live one) with audit writes
stubbed out, so nothing lands in the real audit_log.

Run: python -m pytest tests/test_custom_agents.py -q
"""

import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import auth_db, deps
from core.auth import Principal
from core.auth_db import (
    CustomAgentSlugTaken,
    db_create_custom_agent,
    db_delete_custom_agent,
    db_fork_custom_agent,
    db_get_custom_agent,
    db_get_custom_agent_by_slug,
    db_list_custom_agents,
    db_update_custom_agent,
)
from core.request_context import reset_tool_allowlist, set_current_user, set_tool_allowlist
from core.roles import known_role_names, resolve_role
from routes.custom_agents import apply_input_template, router as custom_agents_router


def _make_principal(user_id=1, username="testuser", is_admin=False, perms=("chat.use",)):
    return Principal(
        id=user_id,
        username=username,
        display_name=username,
        is_super_admin=is_admin,
        must_change_password=False,
        role_names=["admin"] if is_admin else ["user"],
        permission_keys=set(perms),
    )


class _TempAuthDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = auth_db._auth_db
        with mock.patch.object(auth_db, "AUTH_DB_FILE", Path(self.tmp.name) / "auth.db"):
            auth_db._auth_db = auth_db._init_auth_db()
        now = time.time()
        for uid, name, admin in ((1, "alice", 0), (2, "bob", 0), (3, "root", 1)):
            auth_db._auth_db.execute(
                "INSERT INTO users (id, username, is_super_admin, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (uid, name, admin, now, now))
        auth_db._auth_db.commit()
        self._audit = mock.patch("core.audit.auth_db.insert_audit", lambda *a, **k: None)
        self._audit.start()

    def tearDown(self):
        self._audit.stop()
        set_current_user(None)
        auth_db._auth_db.close()
        auth_db._auth_db = self._saved
        self.tmp.cleanup()


class CustomAgentsDBTests(_TempAuthDb):
    def test_starter_agents_seeded(self):
        slugs = [a["slug"] for a in db_list_custom_agents()]
        for s in ("email-analyzer", "system-reporter", "test-qa-automator"):
            self.assertIn(s, slugs)

    def test_crud_and_isolation(self):
        created = db_create_custom_agent(user_id=1, data={
            "name": "Invoice Sorter", "slug": "invoice-sorter", "description": "Sorts invoices.",
            "system_prompt": "You sort invoices.", "tool_allowlist": ["read_file", "doc_inspect"],
            "reasoning_effort": "low", "temperature": 0.1,
        })
        agent_id = created["id"]
        self.assertEqual(created["slug"], "invoice-sorter")
        self.assertEqual(created["tool_allowlist"], ["read_file", "doc_inspect"])
        self.assertEqual(db_get_custom_agent_by_slug("INVOICE-SORTER", user_id=1)["id"], agent_id)

        # user 2 can't see, edit or fork the private agent
        self.assertNotIn("invoice-sorter", [a["slug"] for a in db_list_custom_agents(2, include_public=False)])
        self.assertIsNone(db_update_custom_agent(agent_id, user_id=2, data={"name": "Hacked"}))
        self.assertIsNone(db_fork_custom_agent(agent_id, user_id=2))

        updated = db_update_custom_agent(agent_id, 1, {"name": "Invoice Sorter Pro",
                                                       "slug": "invoice-sorter-pro", "temperature": 0.2})
        self.assertEqual(updated["slug"], "invoice-sorter-pro")
        self.assertAlmostEqual(updated["temperature"], 0.2)

        # public -> forkable by others
        db_update_custom_agent(agent_id, 1, {"share": True, "can_approve": True})
        forked = db_fork_custom_agent(agent_id, user_id=2, new_name="My Invoices")
        self.assertEqual(forked["user_id"], 2)

        self.assertTrue(db_delete_custom_agent(agent_id, user_id=1))
        self.assertIsNone(db_get_custom_agent(agent_id, user_id=1))

    def test_slug_sanitized_and_case_insensitive_unique(self):
        a = db_create_custom_agent(1, {"name": "x", "slug": "My Agent!!", "system_prompt": "p"})
        self.assertEqual(a["slug"], "my-agent")
        b = db_create_custom_agent(1, {"name": "y", "slug": "MY-AGENT", "system_prompt": "p"})
        self.assertEqual(b["slug"], "my-agent-2")
        auto = db_create_custom_agent(1, {"name": "Auto Slug Agent & More!"})
        self.assertEqual(auto["slug"], "auto-slug-agent-more")

    def test_update_slug_collision_raises(self):
        db_create_custom_agent(1, {"name": "a", "slug": "alpha", "system_prompt": "p"})
        b = db_create_custom_agent(1, {"name": "b", "slug": "beta", "system_prompt": "p"})
        with self.assertRaises(CustomAgentSlugTaken):
            db_update_custom_agent(b["id"], 1, {"slug": "Alpha"})

    def test_update_null_keeps_value(self):
        a = db_create_custom_agent(1, {"name": "a", "system_prompt": "keep me"})
        upd = db_update_custom_agent(a["id"], 1, {"system_prompt": None, "description": None})
        self.assertEqual(upd["system_prompt"], "keep me")
        self.assertNotEqual(upd["description"], "None")

    def test_super_admin_edit_returns_row(self):
        a = db_create_custom_agent(1, {"name": "private", "system_prompt": "p"})
        upd = db_update_custom_agent(a["id"], 3, {"name": "renamed by admin"})
        self.assertIsNotNone(upd)
        self.assertEqual(upd["name"], "renamed by admin")

    def test_public_agents_never_resolve_by_slug_for_other_users(self):
        a = db_create_custom_agent(1, {"name": "p", "slug": "shared-thing", "system_prompt": "evil", "share_status": "approved"})
        self.assertIsNotNone(db_get_custom_agent(a["id"], 2))            # pickable in the UI
        self.assertIsNone(db_get_custom_agent_by_slug("shared-thing", 2))  # but not via spawn_agent
        set_current_user(2)
        self.assertNotIn("system_prompt", resolve_role("shared-thing") or {})
        self.assertNotIn("shared-thing", known_role_names())

    def test_role_resolution(self):
        set_current_user(1)
        db_create_custom_agent(1, {"name": "Math Solver", "slug": "math-solver", "system_prompt": "You are a mathematician.",
                                   "tool_allowlist": ["run_python"], "preferred_lane": "auto"})
        role_cfg = resolve_role("math-solver")
        self.assertEqual(role_cfg["tools"], ["run_python"])
        self.assertIn("mathematician", role_cfg["system_prompt"])
        self.assertIsNone(role_cfg["lane"])      # auto -> Settings "Sub-agents" job mapping
        self.assertIn("math-solver", known_role_names())

    def test_seed_refreshes_unedited_templates_only(self):
        conn = auth_db._auth_db
        conn.execute("UPDATE user_custom_agents SET system_prompt = 'stale' WHERE slug = 'email-analyzer'")
        conn.execute("UPDATE user_custom_agents SET system_prompt = 'admin edit', updated_at = updated_at + 5 "
                     "WHERE slug = 'system-reporter'")
        conn.commit()
        auth_db.seed_starter_custom_agents()
        rows = {r["slug"]: r["system_prompt"] for r in conn.execute(
            "SELECT slug, system_prompt FROM user_custom_agents WHERE user_id IS NULL")}
        self.assertNotEqual(rows["email-analyzer"], "stale")
        self.assertEqual(rows["system-reporter"], "admin edit")


class ToolAllowlistEnforcementTests(unittest.TestCase):
    def test_run_tool_refuses_tools_outside_allowlist(self):
        from core.agent_loop import run_tool
        tok = set_tool_allowlist(["read_file"])
        try:
            res = asyncio.run(run_tool("write_file", {"path": "x.txt", "content": "hi"}))
        finally:
            reset_tool_allowlist(tok)
        self.assertTrue(res.startswith("error:"))
        self.assertIn("not enabled", res)

    def test_no_allowlist_means_no_limit(self):
        from core.request_context import tool_allowed
        tok = set_tool_allowlist(None)
        try:
            self.assertTrue(tool_allowed("anything"))
        finally:
            reset_tool_allowlist(tok)

    def test_input_template_wraps_first_turn_only(self):
        agent = {"input_template": "Analyze:\n{input}"}
        first = [{"role": "system", "content": "s"}, {"role": "user", "content": "hello"}]
        apply_input_template(first, agent)
        self.assertEqual(first[1]["content"], "Analyze:\nhello")
        later = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
                 {"role": "user", "content": "c"}]
        apply_input_template(later, agent)
        self.assertEqual(later[2]["content"], "c")


class CustomAgentsAPITests(_TempAuthDb):
    def setUp(self):
        super().setUp()
        app = FastAPI()
        app.include_router(custom_agents_router)
        self.current_user = _make_principal(user_id=1, username="alice")
        app.dependency_overrides[deps.get_current_user] = lambda: self.current_user
        self.client = TestClient(app)

    def _create(self, **kw):
        payload = {"name": "API Tester Agent", "description": "d", "system_prompt": "You test APIs.",
                   "tool_allowlist": ["web_fetch"], "temperature": 0.3}
        payload.update(kw)
        return self.client.post("/custom-agents", json=payload)

    def test_api_crud(self):
        res = self.client.get("/custom-agents")
        self.assertEqual(res.status_code, 200)
        scopes = {a["scope"] for a in res.json()["agents"]}
        self.assertIn("template", scopes)

        created = self._create(slug="api-tester-agent")
        self.assertEqual(created.status_code, 200)
        body = created.json()
        self.assertTrue(body["owned"])
        self.assertEqual(body["scope"], "mine")
        aid = body["id"]

        upd = self.client.put(f"/custom-agents/{aid}", json={"name": "v2", "temperature": 0.0})
        self.assertEqual(upd.status_code, 200)
        self.assertEqual(upd.json()["temperature"], 0.0)

        fork = self.client.post(f"/custom-agents/{aid}/fork", json={"name": "Fork"})
        self.assertEqual(fork.status_code, 200)

        self.assertEqual(self.client.delete(f"/custom-agents/{aid}").json(), {"ok": True})
        self.assertEqual(self.client.get(f"/custom-agents/{aid}").status_code, 404)

    def test_slug_collision_is_409(self):
        self._create(slug="one")
        two = self._create(slug="two").json()
        res = self.client.put(f"/custom-agents/{two['id']}", json={"slug": "one"})
        self.assertEqual(res.status_code, 409)

    def test_reserved_role_slug_rejected(self):
        with mock.patch("routes.custom_agents._reserved_slugs", return_value={"coder"}):
            res = self._create(slug="coder")
        self.assertEqual(res.status_code, 409)

    def test_sharing_waits_for_approval(self):
        body = self._create(is_public=True).json()
        self.assertEqual(body["share_status"], "pending")
        self.assertFalse(body["is_public"])
        # nobody else can see it yet
        self.assertIsNone(db_get_custom_agent(body["id"], 2))
        # an ordinary user cannot review
        self.assertEqual(self.client.get("/custom-agents/pending").status_code, 403)
        self.assertEqual(self.client.post(f"/custom-agents/{body['id']}/approve").status_code, 403)
        # someone with the publish permission can
        self.current_user = _make_principal(5, "carol", perms=("chat.use", "custom_agents.publish"))
        pending = self.client.get("/custom-agents/pending").json()["agents"]
        self.assertEqual([a["id"] for a in pending], [body["id"]])
        self.assertEqual(pending[0]["owner_name"], "alice")
        self.assertEqual(self.client.post(f"/custom-agents/{body['id']}/approve").status_code, 200)
        self.assertIsNotNone(db_get_custom_agent(body["id"], 2))

    def test_editing_an_approved_agent_needs_approval_again(self):
        self.current_user = _make_principal(1, "alice", perms=("chat.use", "custom_agents.publish"))
        aid = self._create(is_public=True).json()["id"]              # a reviewer sharing is approved at once
        self.assertEqual(self.client.get(f"/custom-agents/{aid}").json()["share_status"], "approved")
        self.current_user = _make_principal(1, "alice")
        same = self.client.put(f"/custom-agents/{aid}", json={"is_public": True, "work_dir": "D:/a/b"}).json()
        self.assertEqual(same["share_status"], "approved")           # a folder change alone is not a content change
        changed = self.client.put(f"/custom-agents/{aid}", json={"is_public": True, "system_prompt": "new"}).json()
        self.assertEqual(changed["share_status"], "pending")
        self.assertFalse(changed["is_public"])

    def test_unsharing_and_rejection(self):
        aid = self._create(is_public=True).json()["id"]
        self.current_user = _make_principal(5, "carol", perms=("chat.use", "custom_agents.publish"))
        self.assertEqual(self.client.post(f"/custom-agents/{aid}/reject").json()["share_status"], "rejected")
        self.assertEqual(self.client.post(f"/custom-agents/{aid}/reject").status_code, 404)   # no longer pending
        self.current_user = _make_principal(1, "alice")
        self.assertEqual(self.client.put(f"/custom-agents/{aid}", json={"is_public": False}).json()["share_status"], "")

    def test_fork_takes_only_the_forkers_folder(self):
        other = db_create_custom_agent(2, {"name": "bob", "system_prompt": "p", "share_status": "approved",
                                           "work_dir": "D:/bob/stuff"})
        f = self.client.post(f"/custom-agents/{other['id']}/fork", json={"work_dir": "D:/alice/mine"}).json()
        self.assertEqual(f["work_dir"].replace("\\", "/"), "D:/alice/mine")
        seen = self.client.get(f"/custom-agents/{other['id']}").json()
        self.assertEqual(seen["work_dir"], "")                          # bob's path is never shown to alice
        bad = self.client.post(f"/custom-agents/{other['id']}/fork", json={"work_dir": "C:/"})
        self.assertEqual(bad.status_code, 400)

    def test_update_missing_is_404_and_foreign_is_403(self):
        self.assertEqual(self.client.put("/custom-agents/99999", json={"name": "x"}).status_code, 404)
        other = db_create_custom_agent(2, {"name": "bob's", "system_prompt": "p"})
        self.assertEqual(self.client.put(f"/custom-agents/{other['id']}", json={"name": "x"}).status_code, 403)

    def test_subagent_warnings(self):
        body = self._create(tool_allowlist=["run_shell", "read_file"]).json()
        self.assertEqual(body["subagent_warnings"], ["run_shell"])

    def test_request_schemas_accept_null_temperature(self):
        from routes.chat import ChatRunRequest
        from routes.agent import AgentRequest
        self.assertIsNone(ChatRunRequest(messages=[], custom_agent_id=1).temperature)
        self.assertIsNone(AgentRequest(messages=[]).temperature)


if __name__ == "__main__":
    unittest.main()
