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


def _principal(perms):
    return Principal(id=1, username="t", display_name="t", is_super_admin=False,
                     must_change_password=False, role_names=[], permission_keys=set(perms))


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg_file = Path(self.tmp.name) / "app.json"
        self.cfg_file.write_text(json.dumps({"capabilities": {"mcp": True, "mcp_servers": {}}}), encoding="utf-8")
        self.saved_caps = copy.deepcopy(APP_CONFIG.get("capabilities", {}))
        APP_CONFIG.setdefault("capabilities", {})["mcp_servers"] = {}
        APP_CONFIG["capabilities"].pop("mcp_allowed_commands", None)
        self.saved_top = APP_CONFIG.pop("mcpServers", None)
        self.keychain = {}
        self.connected = []

        async def fake_connect(name, cfg):
            self.connected.append((name, cfg))
            return {"name": name, "status": "ready", "tools": []}

        self.patches = [
            mock.patch.object(mm, "CONFIG_FILE", self.cfg_file),
            mock.patch.object(mm, "audit_log", lambda *a, **k: None),
            mock.patch.object(deps, "audit_log", lambda *a, **k: None),
            mock.patch.object(mm.credentials, "set_token", lambda k, v: self.keychain.__setitem__(k, v)),
            mock.patch.object(mm.credentials, "delete_token", lambda k: self.keychain.pop(k, None)),
            mock.patch.object(mm.mcp_core, "connect_one", fake_connect),
            mock.patch.object(mm.mcp_core, "disconnect_one", lambda n: None),
        ]
        for p in self.patches:
            p.start()
        app = FastAPI()
        app.include_router(mm.router)
        self.perms = {"settings.orchestration.configure"}
        app.dependency_overrides[deps.get_current_user] = lambda: _principal(self.perms)
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


if __name__ == "__main__":
    unittest.main()
