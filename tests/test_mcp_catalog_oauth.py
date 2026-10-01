"""tests/test_mcp_catalog_oauth.py - E2: MCP catalog + OAuth endpoint contracts.

catalog_endpoints.py (23%) and oauth_endpoints.py (19%) are the thinnest
covered route modules. These tests pin the validation and state behaviour
that needs no network: unknown-connector 404s, transport/auth-type gates,
disconnect/state round-trips, and the OAuth callback's owner check.

Run: python -m unittest tests.test_mcp_catalog_oauth -v
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import deps
from core.auth import Principal
from routes.mcp_manager import catalog_endpoints, oauth_endpoints
from routes.mcp_manager.base import MANAGE_PERM, router


def _principal(uid=7, perms=None):
    return Principal(id=uid, username=f"u{uid}", display_name=f"u{uid}", is_super_admin=False,
                     must_change_password=False, role_names=[], permission_keys=set(perms or {"chat.use"}))


class _McpApp(unittest.TestCase):
    perms = {"chat.use", MANAGE_PERM}

    def setUp(self):
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[deps.get_current_user] = lambda: _principal(7, self.perms)
        self.patches = [
            mock.patch.object(catalog_endpoints, "audit_log", lambda *a, **k: None),
            mock.patch.object(oauth_endpoints, "audit_log", lambda *a, **k: None),
        ]
        for p in self.patches:
            p.start()
        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self):
        self.client.close()
        for p in self.patches:
            p.stop()


class CatalogTests(_McpApp):
    def test_authorize_unknown_connector_404(self):
        with mock.patch.object(catalog_endpoints.mcp_catalog, "get_entry", lambda sid: None):
            r = self.client.post("/mcp/nope/authorize/start")
        self.assertEqual(r.status_code, 404)

    def test_authorize_non_device_flow_400(self):
        entry = {"id": "x", "auth": {"type": "oauth"}}
        with mock.patch.object(catalog_endpoints.mcp_catalog, "get_entry", lambda sid: entry):
            r = self.client.post("/mcp/x/authorize/start")
        self.assertEqual(r.status_code, 400)
        self.assertIn("device-flow", r.json()["error"])

    def test_authorize_missing_client_id_400(self):
        entry = {"id": "x", "auth": {"type": "device_flow"}}
        with mock.patch.object(catalog_endpoints.mcp_catalog, "get_entry", lambda sid: entry):
            r = self.client.post("/mcp/x/authorize/start")
        self.assertEqual(r.status_code, 400)
        self.assertIn("client_id", r.json()["error"])

    def test_disable_unknown_404_then_ok(self):
        with mock.patch.object(catalog_endpoints.mcp_catalog, "get_entry", lambda sid: None):
            self.assertEqual(self.client.post("/mcp/nope/disable").status_code, 404)
        entry = {"id": "x"}
        with mock.patch.object(catalog_endpoints.mcp_catalog, "get_entry", lambda sid: entry), \
             mock.patch.object(catalog_endpoints.mcp_core, "disconnect_one") as disc, \
             mock.patch.object(catalog_endpoints.credentials, "delete_token") as dt, \
             mock.patch.object(catalog_endpoints, "_remove_server_entry") as rm:
            r = self.client.post("/mcp/x/disable")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        disc.assert_called_once_with("x")
        dt.assert_called_once_with("x")
        rm.assert_called_once_with("x")

    def test_needs_manage_perm(self):
        self.perms = {"chat.use"}
        self.assertEqual(self.client.post("/mcp/x/disable").status_code, 403)


class OAuthTests(_McpApp):
    def test_start_unknown_server_404(self):
        with mock.patch.object(oauth_endpoints, "_servers_for", lambda owner: {}):
            r = self.client.post("/mcp/servers/nope/oauth/start", json={"scope": "user"})
        self.assertEqual(r.status_code, 404)

    def test_start_non_http_400(self):
        scfg = {"transport": "stdio", "command": "npx foo"}
        with mock.patch.object(oauth_endpoints, "_servers_for", lambda owner: {"s": scfg}):
            r = self.client.post("/mcp/servers/s/oauth/start", json={"scope": "user"})
        self.assertEqual(r.status_code, 400)

    def test_start_without_companion_or_redirect_base_409(self):
        from core import companion_bridge
        scfg = {"transport": "http", "url": "http://x"}
        with mock.patch.object(oauth_endpoints, "_servers_for", lambda owner: {"s": scfg}), \
             mock.patch.object(companion_bridge, "is_connected", lambda uid: False), \
             mock.patch.object(oauth_endpoints, "_redirect_base", lambda: ""):
            r = self.client.post("/mcp/servers/s/oauth/start", json={"scope": "user"})
        self.assertEqual(r.status_code, 409)

    def test_disconnect_round_trip(self):
        with mock.patch.object(oauth_endpoints, "_servers_for", lambda owner: {}):
            pass
        r = self.client.post("/mcp/servers/s/oauth/disconnect", json={"scope": "user"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        r = self.client.get("/mcp/servers/s/oauth/status", params={"scope": "user"})
        self.assertEqual(r.status_code, 200)

    def test_callback_error_is_400(self):
        r = self.client.get("/mcp/oauth/callback", params={"error": "denied"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("denied", r.text)

    def test_callback_for_other_account_is_403(self):
        from core import mcp_oauth
        mcp_oauth._flows["st9"] = {"owner": 4242, "name": "s"}
        try:
            r = self.client.get("/mcp/oauth/callback", params={"state": "st9", "code": "c"})
        finally:
            mcp_oauth._flows.pop("st9", None)
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
