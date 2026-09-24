"""tests/test_mcp_manager_routes.py - Settings UI endpoints for adding/editing/removing MCP servers.

Run: python -m unittest tests.test_mcp_manager_routes -v
"""

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import deps
from core.auth import Principal
from core.small_model import APP_CONFIG
from routes import mcp_manager as mm

SECRET = "[PLACEHOLDER_API_KEY]"


def _principal(perms, uid=1):
    return Principal(id=uid, username="t", display_name="t", is_super_admin=False,
                     must_change_password=False, role_names=[], permission_keys=set(perms))


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg_file = Path(self.tmp.name) / "app.json"
        self.cfg_file.write_text(json.dumps({"capabilities": {"mcp": True, "mcp_servers": {}}}), encoding="utf-8")
        self.saved_caps = copy.deepcopy(APP_CONFIG.get("capabilities", {}))
        APP_CONFIG.setdefault("capabilities", {})["mcp_servers"] = {}
        APP_CONFIG["capabilities"].pop("mcp_allowed_commands", None)
        APP_CONFIG["capabilities"]["mcp_user_allowed_packages"] = ["mcp-atlassian", "@acme/jira-mcp"]
        self.saved_top = APP_CONFIG.pop("mcpServers", None)
        self.keychain = {}
        self.connected = []

        self.user_db = {}   # (user_id, name) -> config, stands in for auth.db user_mcp_servers

        async def fake_connect(name, cfg, owner=None):
            self.connected.append((name, cfg, owner))
            return {"name": name, "status": "ready", "tools": []}

        self.patches = [
            mock.patch.object(mm, "CONFIG_FILE", self.cfg_file),
            mock.patch.object(mm, "audit_log", lambda *a, **k: None),
            mock.patch.object(deps, "audit_log", lambda *a, **k: None),
            mock.patch.object(mm.credentials, "set_token", lambda k, v: self.keychain.__setitem__(k, v)),
            mock.patch.object(mm.credentials, "get_token", lambda k: self.keychain.get(k)),
            mock.patch.object(mm.credentials, "delete_token", lambda k: self.keychain.pop(k, None)),
            mock.patch.object(mm.mcp_core, "connect_one", fake_connect),
            mock.patch.object(mm.mcp_core, "disconnect_one", lambda n, owner=None: None),
            mock.patch.object(mm.auth_db, "upsert_user_mcp_server",
                              lambda uid, n, c: self.user_db.__setitem__((uid, n), c)),
            mock.patch.object(mm.auth_db, "delete_user_mcp_server", lambda uid, n: self.user_db.pop((uid, n), None)),
            mock.patch.object(mm.mcp_core, "user_servers",
                              lambda uid: {n: c for (u, n), c in self.user_db.items() if u == uid}),
        ]
        for p in self.patches:
            p.start()
        app = FastAPI()
        app.include_router(mm.router)
        self.perms = {"settings.orchestration.configure", "chat.use"}
        self.uid = 1
        app.dependency_overrides[deps.get_current_user] = lambda: _principal(self.perms, self.uid)
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        for p in self.patches:
            p.stop()
        APP_CONFIG["capabilities"] = self.saved_caps
        if self.saved_top is not None:
            APP_CONFIG["mcpServers"] = self.saved_top
        self.tmp.cleanup()

    def _file(self):
        return json.loads(self.cfg_file.read_text(encoding="utf-8"))

    def test_add_stdio_server_persists_and_connects(self):
        r = self.client.post("/mcp/servers", json={
            "name": "sslcommerz", "transport": "stdio", "command": "npx",
            "args": ["-y", "mcp-remote", "https://sslcommerz-mcp.example/mcp"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"]["status"], "connecting")
        saved = self._file()["capabilities"]["mcp_servers"]["sslcommerz"]
        self.assertEqual(saved["command"], "npx")
        self.assertEqual(self.connected[0][0], "sslcommerz")

    def test_rejects_bad_name_command_and_url(self):
        bad = [
            {"name": "Bad Name!", "command": "npx"},
            {"name": "x", "command": "powershell", "args": ["-c", "whoami"]},
            {"name": "y", "transport": "http", "url": "http://evil.example/mcp"},
            {"name": "z", "transport": "http", "url": "file:///etc/passwd"},
        ]
        for body in bad:
            r = self.client.post("/mcp/servers", json=body)
            self.assertEqual(r.status_code, 400, body)
        self.assertEqual(self._file()["capabilities"]["mcp_servers"], {})

    def test_localhost_http_allowed(self):
        r = self.client.post("/mcp/servers", json={"name": "loc", "transport": "http", "url": "http://127.0.0.1:9000/mcp"})
        self.assertEqual(r.status_code, 200, r.text)

    def test_secret_env_never_written_or_returned(self):
        r = self.client.post("/mcp/servers", json={
            "name": "k", "command": "npx", "args": ["srv"], "env": {"MODE": "ro"}, "secret_env": {"API_KEY": SECRET}})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(SECRET, r.text)
        self.assertNotIn(SECRET, self.cfg_file.read_text(encoding="utf-8"))
        self.assertEqual(self.keychain["k:env:API_KEY"], SECRET)
        self.assertEqual(self._file()["capabilities"]["mcp_servers"]["k"]["secret_env_keys"], ["API_KEY"])
        listing = self.client.get("/mcp/servers")
        self.assertNotIn(SECRET, listing.text)

        # edit: blank secret keeps the stored value, omitted secret is deleted
        r = self.client.put("/mcp/servers/k", json={"name": "k", "command": "npx", "args": ["srv"],
                                                     "secret_env": {"API_KEY": ""}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.keychain["k:env:API_KEY"], SECRET)
        self.assertEqual(self._file()["capabilities"]["mcp_servers"]["k"]["secret_env_keys"], ["API_KEY"])
        self.client.put("/mcp/servers/k", json={"name": "k", "command": "npx", "args": ["srv"]})
        self.assertNotIn("k:env:API_KEY", self.keychain)

    def test_duplicate_and_delete(self):
        body = {"name": "d", "command": "node", "args": ["s.js"]}
        self.assertEqual(self.client.post("/mcp/servers", json=body).status_code, 200)
        self.assertEqual(self.client.post("/mcp/servers", json=body).status_code, 409)
        self.assertEqual(self.client.delete("/mcp/servers/d").status_code, 200)
        self.assertNotIn("d", self._file()["capabilities"]["mcp_servers"])
        self.assertEqual(self.client.delete("/mcp/servers/d").status_code, 404)

    def test_import_claude_desktop_block(self):
        block = {"mcpServers": {
            "sslcommerz": {"command": "npx", "args": ["mcp-remote", "https://a.example/mcp"], "disabled": False},
            "vr": {"command": "npx", "args": ["mcp-remote", "https://b.example/sse"], "disabled": True},
            "evil": {"command": "cmd.exe", "args": ["/c", "calc"]},
        }}
        r = self.client.post("/mcp/import", json={"config": block})
        self.assertEqual(r.status_code, 200, r.text)
        res = {x["name"]: x for x in r.json()["results"]}
        self.assertEqual(res["sslcommerz"]["status"]["status"], "connecting")
        self.assertEqual(res["vr"]["status"]["status"], "disabled")
        self.assertIn("not allowed", res["evil"]["error"])
        servers = self._file()["capabilities"]["mcp_servers"]
        self.assertEqual(set(servers), {"sslcommerz", "vr"})
        self.assertTrue(servers["vr"]["disabled"])

    def test_requires_permission(self):
        self.perms = set()
        self.assertEqual(self.client.post("/mcp/servers", json={"name": "p", "command": "npx"}).status_code, 403)
        self.assertEqual(self.client.get("/mcp/servers").status_code, 403)
        self.assertEqual(self.client.post("/mcp/import", json={"config": {"mcpServers": {}}}).status_code, 403)

    # ---------------- personal ("just for me") servers ----------------

    def _as_user(self, uid=7):
        self.perms, self.uid = {"chat.use"}, uid

    def test_user_cannot_touch_global(self):
        self._as_user()
        body = {"name": "g", "transport": "http", "url": "https://mcp.example.com/mcp"}
        self.assertEqual(self.client.post("/mcp/servers", json=body).status_code, 403)
        self.assertEqual(self.client.post("/mcp/servers", json={**body, "scope": "global"}).status_code, 403)
        self.assertEqual(self.client.delete("/mcp/servers/g?scope=global").status_code, 403)
        self.assertEqual(self.client.post("/mcp/import", json={"config": {"mcpServers": {"g": body}}}).status_code, 403)
        self.assertEqual(self.client.put("/mcp/user-packages", json={"packages": ["x"]}).status_code, 403)
        self.assertEqual(self._file()["capabilities"]["mcp_servers"], {})

    def test_user_adds_personal_approved_package(self):
        self._as_user()
        r = self.client.post("/mcp/servers", json={
            "name": "jira", "scope": "user", "command": "uvx", "args": ["mcp-atlassian"],
            "env": {"JIRA_URL": "https://[PLACEHOLDER].atlassian.net"}, "secret_env": {"JIRA_API_TOKEN": SECRET}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["server"]["scope"], "user")
        self.assertNotIn(SECRET, r.text)
        self.assertIn((7, "jira"), self.user_db)
        self.assertNotIn(SECRET, json.dumps(self.user_db[(7, "jira")]))
        self.assertEqual(self.keychain["u7:jira:env:JIRA_API_TOKEN"], SECRET)
        self.assertEqual(self._file()["capabilities"]["mcp_servers"], {})   # never in app.json
        self.assertEqual(self.connected[-1][0], "jira")
        self.assertEqual(self.connected[-1][2], 7)
        # npx form with a pinned version and -y is fine too
        r = self.client.post("/mcp/servers", json={"name": "j2", "scope": "user", "command": "npx",
                                                    "args": ["-y", "@acme/jira-mcp@1.2.0", "--read-only"]})
        self.assertEqual(r.status_code, 200, r.text)

    def test_user_restrictions(self):
        self._as_user()
        bad = [
            {"name": "a", "command": "python", "args": ["-c", "print(1)"]},
            {"name": "b", "command": "npx", "args": ["-y", "some-other-pkg"]},
            {"name": "c", "command": "npx", "args": ["--package", "evil", "mcp-atlassian"]},
            {"name": "d", "command": "uvx", "args": ["--from", "evil", "mcp-atlassian"]},
            {"name": "e", "command": "node", "args": ["srv.js"]},
            {"name": "f", "command": "uvx", "args": ["mcp-atlassian"], "env": {"UV_INDEX_URL": "https://evil.example"}},
            {"name": "g", "command": "npx", "args": ["mcp-atlassian"], "env": {"node_options": "--require x.js"}},
            {"name": "h", "command": "uvx", "args": ["mcp-atlassian"], "secret_env": {"PATH": "x"}},
            {"name": "i", "transport": "http", "url": "http://127.0.0.1:9000/mcp"},
            {"name": "j", "transport": "http", "url": "https://localhost/mcp"},
            {"name": "k", "transport": "http", "url": "https://10.0.0.5/mcp"},
            {"name": "l", "transport": "http", "url": "https://169.254.169.254/latest"},
        ]
        for body in bad:
            r = self.client.post("/mcp/servers", json={**body, "scope": "user"})
            self.assertEqual(r.status_code, 400, body)
        self.assertEqual(self.user_db, {})
        r = self.client.post("/mcp/servers", json={"name": "remote", "scope": "user", "transport": "http",
                                                    "url": "https://mcp.example.com/mcp"})
        self.assertEqual(r.status_code, 200, r.text)

    def test_personal_isolated_between_users(self):
        self._as_user(7)
        self.client.post("/mcp/servers", json={"name": "mine", "scope": "user", "transport": "http",
                                               "url": "https://mcp.example.com/mcp"})
        self._as_user(8)
        self.assertEqual(self.client.get("/mcp/servers").json()["servers"], [])
        self.assertEqual(self.client.delete("/mcp/servers/mine?scope=user").status_code, 404)
        self.assertEqual(self.client.post("/mcp/servers/mine/reconnect?scope=user").status_code, 404)
        self.assertIn((7, "mine"), self.user_db)
        self._as_user(7)
        listing = self.client.get("/mcp/servers").json()
        self.assertEqual([(s["name"], s["scope"]) for s in listing["servers"]], [("mine", "user")])
        self.assertFalse(listing["can_manage_global"])
        self.assertEqual(self.client.delete("/mcp/servers/mine?scope=user").status_code, 200)
        self.assertEqual(self.user_db, {})

    def test_admin_picks_scope(self):
        # admin: personal servers skip the package allowlist, global name clash is refused
        self.client.post("/mcp/servers", json={"name": "shared", "command": "npx", "args": ["srv"]})
        r = self.client.post("/mcp/servers", json={"name": "shared", "scope": "user", "command": "npx", "args": ["srv"]})
        self.assertEqual(r.status_code, 409)
        r = self.client.post("/mcp/servers", json={"name": "own", "scope": "user", "command": "node", "args": ["s.js"],
                                                    "secret_env": {"K": SECRET}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.keychain["u1:own:env:K"], SECRET)
        self.assertNotIn("own", self._file()["capabilities"]["mcp_servers"])
        listing = self.client.get("/mcp/servers").json()
        self.assertTrue(listing["can_manage_global"])
        self.assertEqual(sorted((s["name"], s["scope"]) for s in listing["servers"]),
                         [("own", "user"), ("shared", "global")])
        # edit personal keeps it personal; bad scope rejected
        r = self.client.put("/mcp/servers/own", json={"name": "own", "scope": "user", "command": "node",
                                                       "args": ["s.js"], "secret_env": {"K": ""}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.user_db[(1, "own")]["secret_env_keys"], ["K"])
        self.assertEqual(self.client.post("/mcp/servers", json={"name": "z", "scope": "team", "command": "npx"}).status_code, 400)

    def test_user_import_personal(self):
        self._as_user()
        block = {"mcpServers": {"jira": {"command": "uvx", "args": ["mcp-atlassian"]},
                                "evil": {"command": "npx", "args": ["-y", "evil-pkg"]}}}
        r = self.client.post("/mcp/import", json={"config": block, "scope": "user"})
        self.assertEqual(r.status_code, 200, r.text)
        res = {x["name"]: x for x in r.json()["results"]}
        self.assertEqual(res["jira"]["status"]["status"], "connecting")
        self.assertIn("not approved", res["evil"]["error"])
        self.assertEqual(set(self.user_db), {(7, "jira")})

    def test_admin_sets_user_packages(self):
        r = self.client.put("/mcp/user-packages", json={"packages": ["mcp-atlassian@1.0", "@acme/pkg", " "]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["packages"], ["@acme/pkg", "mcp-atlassian"])
        self.assertEqual(self._file()["capabilities"]["mcp_user_allowed_packages"], ["@acme/pkg", "mcp-atlassian"])
    def test_admin_changes_scope_on_edit(self):
        # 1. Start with personal server with secret token
        r = self.client.post("/mcp/servers", json={"name": "scoped", "scope": "user", "command": "npx",
                                                    "args": ["srv"], "secret_env": {"API_KEY": SECRET}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn((1, "scoped"), self.user_db)
        self.assertNotIn("scoped", self._file()["capabilities"]["mcp_servers"])
        self.assertEqual(self.keychain["u1:scoped:env:API_KEY"], SECRET)

        # 2. Change availability from user to global (keep secret)
        r = self.client.put("/mcp/servers/scoped?from_scope=user", json={
            "name": "scoped", "scope": "global", "from_scope": "user", "command": "npx",
            "args": ["srv"], "secret_env": {"API_KEY": ""}
        })
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn((1, "scoped"), self.user_db)
        self.assertIn("scoped", self._file()["capabilities"]["mcp_servers"])
        self.assertEqual(self.keychain.get("scoped:env:API_KEY"), SECRET)
        self.assertNotIn("u1:scoped:env:API_KEY", self.keychain)

        # 3. Change availability back from global to user
        r = self.client.put("/mcp/servers/scoped?from_scope=global", json={
            "name": "scoped", "scope": "user", "from_scope": "global", "command": "npx",
            "args": ["srv"], "secret_env": {"API_KEY": ""}
        })
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn((1, "scoped"), self.user_db)
        self.assertNotIn("scoped", self._file()["capabilities"]["mcp_servers"])
        self.assertEqual(self.keychain.get("u1:scoped:env:API_KEY"), SECRET)
        self.assertNotIn("scoped:env:API_KEY", self.keychain)


if __name__ == "__main__":
    unittest.main()
