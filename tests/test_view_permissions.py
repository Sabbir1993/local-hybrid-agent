"""tests/test_view_permissions.py - skills.view, connectors.view, plugins.view, agents.view and database.view.

Each one decides what a person sees in the navbar (Customize tabs, My Agents, Database) and, here, what the server lets them
read. The seeding tests cover the upgrade rule: nobody loses a feature they had.

Run: python -m unittest tests.test_view_permissions -v
"""

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from core import auth_db, deps
from core.auth import Principal
from core.request_context import set_current_user

VIEW_KEYS = ("skills.view", "connectors.view", "plugins.view", "agents.view")


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

    def add_role(self, name, *keys):
        now = time.time()
        self.db.execute("INSERT INTO roles (name, description, is_builtin, created_at) VALUES (?, '', 0, ?)", (name, now))
        for k in keys:
            self.db.execute("INSERT INTO role_permissions (role_id, permission_id, granted_at) "
                            "SELECT r.id, p.id, ? FROM roles r, permissions p WHERE r.name = ? AND p.key = ?", (now, name, k))
        self.db.commit()

    def forget(self, *keys):
        """An install from before these keys existed: no key rows, no grants."""
        for k in keys:
            self.db.execute("DELETE FROM role_permissions WHERE permission_id IN (SELECT id FROM permissions WHERE key = ?)", (k,))
            self.db.execute("DELETE FROM permissions WHERE key = ?", (k,))
        self.db.commit()


class SeedTests(_TempAuthDb):
    def test_fresh_install_user_sees_the_customize_tabs_but_not_the_database(self):
        for k in VIEW_KEYS:
            self.assertIn(k, _perm_keys("user"))
        self.assertNotIn("database.view", _perm_keys("user"))
        self.assertNotIn("database.manage", _perm_keys("user"))

    def test_admin_gets_all_five(self):
        for k in (*VIEW_KEYS, "database.view"):
            self.assertIn(k, _perm_keys("admin"))

    def test_upgrade_keeps_current_access(self):
        self.add_role("team_a", "chat.use")
        self.add_role("ops", "chat.use", "database.manage")
        self.add_role("auditor", "audit.view")                 # no chat.use, no database.manage
        self.forget(*VIEW_KEYS, "database.view")
        auth_db.init_tables(self.db)
        for k in VIEW_KEYS:
            self.assertIn(k, _perm_keys("team_a"), k)           # everyone who could chat keeps the tabs
            self.assertNotIn(k, _perm_keys("auditor"), k)
        self.assertIn("database.view", _perm_keys("ops"))       # the broader key implies the narrower one
        self.assertNotIn("database.view", _perm_keys("team_a"))
        self.assertNotIn("database.view", _perm_keys("auditor"))

    def test_an_admin_revoke_survives_a_restart(self):
        self.add_role("team_a", "chat.use", *VIEW_KEYS)
        self.db.execute("DELETE FROM role_permissions WHERE role_id = (SELECT id FROM roles WHERE name = 'team_a') AND "
                        "permission_id = (SELECT id FROM permissions WHERE key = 'plugins.view')")
        self.db.commit()
        auth_db.init_tables(self.db)
        auth_db.init_tables(self.db)
        self.assertNotIn("plugins.view", _perm_keys("team_a"))
        self.assertIn("skills.view", _perm_keys("team_a"))

    def test_api_tokens_never_carry_database_view(self):
        from core.auth import API_TOKEN_FORBIDDEN_PERMS
        self.assertIn("database.view", API_TOKEN_FORBIDDEN_PERMS)


def _principal(perms, is_super=False):
    return Principal(id=7, username="u7", display_name="u7", is_super_admin=is_super,
                     must_change_password=False, role_names=[], permission_keys=set(perms))


class RouteTests(unittest.TestCase):
    """The real app, with the session replaced by a principal that holds exactly `perms`."""

    @classmethod
    def setUpClass(cls):
        from server_manager import app
        cls.app = app

    def get(self, method, url, perms, **kw):
        self.app.dependency_overrides[deps.get_current_user] = lambda: _principal(perms)
        self.addCleanup(self.app.dependency_overrides.pop, deps.get_current_user, None)
        c = TestClient(self.app, raise_server_exceptions=False)
        self.addCleanup(c.close)
        with mock.patch.object(deps, "audit_log"), mock.patch.object(deps.auth_db, "totp_enabled", return_value=False), \
                mock.patch("routes.customize.base.audit_log"):
            return getattr(c, method)(url, **kw)

    def assertDenied(self, r, key):
        self.assertEqual(r.status_code, 403, r.text[:200])
        self.assertIn(key, r.text)

    def test_customize_tabs_are_gated_one_by_one(self):
        for kind, key in (("skills", "skills.view"), ("plugins", "plugins.view"), ("connectors", "connectors.view")):
            with self.subTest(kind=kind):
                self.assertDenied(self.get("get", f"/customize/{kind}", {"chat.use"}), key)
                others = {k for k in VIEW_KEYS if k != key}
                self.assertDenied(self.get("get", f"/customize/{kind}", others), key)
                self.assertEqual(self.get("get", f"/customize/{kind}", {key}).status_code, 200)

    def test_previews_and_registries_are_gated(self):
        self.assertDenied(self.get("get", "/customize/skills/anything/preview", {"plugins.view"}), "skills.view")
        self.assertDenied(self.get("get", "/customize/connectors/registry", {"skills.view"}), "connectors.view")
        self.assertDenied(self.get("get", "/customize/plugins/registry", {"skills.view"}), "plugins.view")
        self.assertDenied(self.get("post", "/customize/plugins/inspect-remote", {"skills.view"},
                                   json={"manifest_url": "https://example.com/m.json"}), "plugins.view")

    def test_connector_and_plugin_catalogs_are_gated(self):
        self.assertDenied(self.get("get", "/mcp/catalog", {"chat.use"}), "connectors.view")
        self.assertDenied(self.get("get", "/mcp/registry/search", {"chat.use"}), "connectors.view")
        self.assertDenied(self.get("get", "/plugins/catalog", {"chat.use"}), "plugins.view")
        self.assertEqual(self.get("get", "/plugins/catalog", {"plugins.view"}).status_code, 200)

    def test_personal_connectors_need_both_chat_and_connectors_view(self):
        self.assertDenied(self.get("get", "/mcp/servers", {"chat.use"}), "connectors.view")
        self.assertEqual(self.get("get", "/mcp/servers", {"connectors.view"}).status_code, 403)    # no chat.use either
        self.assertNotEqual(self.get("get", "/mcp/servers", {"chat.use", "connectors.view"}).status_code, 403)

    def test_custom_agents_and_the_agent_library_need_agents_view(self):
        self.assertDenied(self.get("get", "/custom-agents", {"chat.use"}), "agents.view")
        self.assertDenied(self.get("get", "/custom-agents/pending", {"chat.use", "custom_agents.publish"}), "agents.view")
        self.assertNotEqual(self.get("get", "/custom-agents", {"agents.view"}).status_code, 403)
        self.assertDenied(self.get("get", "/control/agent_library", {"chat.use"}), "agents.view")
        self.assertEqual(self.get("get", "/control/agent_library", {"agents.view"}).status_code, 200)
        # the admins who edit the library keep reading it
        self.assertEqual(self.get("get", "/control/agent_library", {"settings.agents.configure"}).status_code, 200)

    def test_database_view_is_read_only_and_scoped(self):
        r = self.get("get", "/db/list", {"database.view"})
        self.assertEqual(r.status_code, 200, r.text[:200])
        ids = {d["id"] for d in r.json()["databases"]}
        self.assertIn("usage", ids)
        self.assertFalse(ids & {"auth", "projects", "memory"}, ids)
        for sid in ("auth", "projects", "memory"):
            self.assertDenied(self.get("get", f"/db/{sid}/schema", {"database.view"}), "database.manage")
            self.assertDenied(self.get("get", f"/db/{sid}/table/users/data", {"database.view"}), "database.manage")
        self.assertNotEqual(self.get("get", "/db/usage/schema", {"database.view"}).status_code, 403)
        # the SQL console stays with database.manage
        self.assertDenied(self.get("post", "/db/usage/query", {"database.view"}, json={"sql": "select 1"}), "database.manage")

    def test_database_manage_still_opens_everything(self):
        ids = {d["id"] for d in self.get("get", "/db/list", {"database.manage"}).json()["databases"]}
        self.assertTrue({"auth", "usage", "projects", "memory"} <= ids)
        self.assertNotEqual(self.get("get", "/db/auth/schema", {"database.manage"}).status_code, 403)
        self.assertEqual(self.get("get", "/db/list", {"chat.use"}).status_code, 403)

    def test_report_and_audit_keep_their_existing_keys(self):
        self.assertDenied(self.get("get", "/control/report", {"chat.use"}), "usage.report.view")
        self.assertDenied(self.get("get", "/admin/audit_log", {"chat.use"}), "audit.view")


if __name__ == "__main__":
    unittest.main()
