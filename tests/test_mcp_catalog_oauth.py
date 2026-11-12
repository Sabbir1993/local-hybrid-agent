"""tests/test_mcp_catalog_oauth.py - E2: MCP catalog + OAuth endpoint contracts.

catalog_endpoints.py (23%) and oauth_endpoints.py (19%) are the thinnest
covered route modules. These tests pin the validation and state behaviour
that needs no network: unknown-connector 404s, transport/auth-type gates,
disconnect/state round-trips, and the OAuth callback's owner check.

Run: python -m unittest tests.test_mcp_catalog_oauth -v
"""

import contextlib
import io
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import deps, mcp_oauth
from core.auth import Principal
from routes.mcp_manager import catalog_endpoints, oauth_endpoints
from routes.mcp_manager.base import MANAGE_PERM, router


def _principal(uid=7, perms=None):
    return Principal(id=uid, username=f"u{uid}", display_name=f"u{uid}", is_super_admin=False,
                     must_change_password=False, role_names=[], permission_keys=set(perms or {"chat.use"}))


def _fake_begin(port=40123, state="st-1"):
    async def fake(name, owner, cfg, redirect_uri=None):
        mcp_oauth._flows[state] = {"name": name, "owner": owner, "expires": time.time() + 600}
        return {"auth_url": "https://auth.example/authorize?response_type=code&redirect_uri="
                            + mcp_oauth.REDIRECT_PLACEHOLDER + "&state=" + state,
                "state": state, "loopback_port": port}
    return fake


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

    def test_start_without_companion_or_redirect_base_is_manual_mode(self):
        from core import companion_bridge
        scfg = {"transport": "http", "url": "http://x"}
        with mock.patch.object(oauth_endpoints, "_servers_for", lambda owner: {"s": scfg}), \
             mock.patch.object(companion_bridge, "is_connected", lambda uid: False), \
             mock.patch.object(oauth_endpoints, "_redirect_base", lambda: ""), \
             mock.patch.object(oauth_endpoints.mcp_oauth, "begin", _fake_begin(port=40123)):
            r = self.client.post("/mcp/servers/s/oauth/start", json={"scope": "user"})
        self.assertEqual(r.status_code, 200, r.text)
        j = r.json()
        self.assertEqual((j["mode"], j["status"]), ("manual", "pending"))
        self.assertEqual(j["state"], "st-1")
        self.assertIn("redirect_uri=http%3A%2F%2F127.0.0.1%3A40123%2Fcallback", j["auth_url"])
        self.assertNotIn("__A770", j["auth_url"])

    def test_start_manual_mode_uses_default_port_when_none_is_fixed(self):
        scfg = {"transport": "http", "url": "http://x"}
        with mock.patch.object(oauth_endpoints, "_servers_for", lambda owner: {"s": scfg}), \
             mock.patch.object(oauth_endpoints, "_redirect_base", lambda: ""), \
             mock.patch.object(oauth_endpoints.mcp_oauth, "begin", _fake_begin(port=0)):
            r = self.client.post("/mcp/servers/s/oauth/start", json={"scope": "user"})
        self.assertIn(f"127.0.0.1%3A{oauth_endpoints.mcp_oauth.DEFAULT_DCR_PORT}%2Fcallback", r.json()["auth_url"])

    def test_start_manual_provider_error_is_surfaced(self):
        async def boom(*a, **k):
            raise RuntimeError("client registration failed (HTTP 400): invalid_redirect_uri")
        scfg = {"transport": "http", "url": "http://x"}
        with mock.patch.object(oauth_endpoints, "_servers_for", lambda owner: {"s": scfg}), \
             mock.patch.object(oauth_endpoints, "_redirect_base", lambda: ""), \
             mock.patch.object(oauth_endpoints.mcp_oauth, "begin", boom):
            r = self.client.post("/mcp/servers/s/oauth/start", json={"scope": "user"})
        self.assertEqual(r.status_code, 502)
        self.assertIn("invalid_redirect_uri", r.json()["error"])

    def test_companion_mode_unchanged(self):
        from core import companion_bridge

        async def noop(*a, **k):
            return None
        scfg = {"transport": "http", "url": "http://x"}
        with mock.patch.object(oauth_endpoints, "_servers_for", lambda owner: {"s": scfg}), \
             mock.patch.object(companion_bridge, "is_connected", lambda uid: True), \
             mock.patch.object(oauth_endpoints, "_loopback_flow", noop):
            r = self.client.post("/mcp/servers/s/oauth/start", json={"scope": "user"})
        self.assertEqual(r.json()["mode"], "companion")

    def test_redirect_mode_unchanged_with_base(self):
        scfg = {"transport": "http", "url": "http://x"}
        seen = {}

        async def fb(name, owner, cfg, redirect_uri=None):
            seen["r"] = redirect_uri
            return {"auth_url": "https://a/authorize", "state": "s"}
        with mock.patch.object(oauth_endpoints, "_servers_for", lambda owner: {"s": scfg}), \
             mock.patch.object(oauth_endpoints, "_redirect_base", lambda: "https://app.example"), \
             mock.patch.object(oauth_endpoints.mcp_oauth, "begin", fb):
            r = self.client.post("/mcp/servers/s/oauth/start", json={"scope": "user"})
        self.assertEqual(r.json()["mode"], "redirect")
        self.assertEqual(seen["r"], "https://app.example/mcp/oauth/callback")

    def test_same_machine_base_only_for_a_loopback_peer_on_a_loopback_host(self):
        from types import SimpleNamespace
        fake = lambda peer, host: SimpleNamespace(client=SimpleNamespace(host=peer), headers={"host": host})  # noqa: E731
        f = oauth_endpoints._same_machine_base
        self.assertEqual(f(fake("127.0.0.1", "127.0.0.1:8000")), "http://127.0.0.1:8000")
        self.assertEqual(f(fake("::1", "localhost:8000")), "http://localhost:8000")
        self.assertEqual(f(fake("192.168.1.9", "127.0.0.1:8000")), "", "a remote peer is never trusted")
        self.assertEqual(f(fake("127.0.0.1", "evil.example.com")), "", "a loopback peer behind some other host name")
        self.assertEqual(f(fake("127.0.0.1", "")), "")
        self.assertEqual(f(SimpleNamespace(client=None, headers={"host": "127.0.0.1:8000"})), "")

    def test_start_on_the_same_machine_uses_the_server_callback_without_a_companion(self):
        from core import companion_bridge
        scfg = {"transport": "http", "url": "http://x"}
        seen = {}

        async def fake_begin(name, owner, cfg, redirect_uri=None):
            seen["redirect"] = redirect_uri
            return {"auth_url": "https://auth.example/authorize?x=1", "state": "s"}

        with mock.patch.object(oauth_endpoints, "_servers_for", lambda owner: {"s": scfg}),              mock.patch.object(companion_bridge, "is_connected", lambda uid: False),              mock.patch.object(oauth_endpoints, "_redirect_base", lambda: ""),              mock.patch.object(oauth_endpoints, "_same_machine_base", lambda request: "http://127.0.0.1:8000"),              mock.patch.object(oauth_endpoints.mcp_oauth, "begin", fake_begin):
            r = self.client.post("/mcp/servers/s/oauth/start", json={"scope": "user"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["mode"], "redirect")
        self.assertEqual(seen["redirect"], "http://127.0.0.1:8000/mcp/oauth/callback")

    def test_disconnect_round_trip(self):
        with mock.patch.object(oauth_endpoints, "_servers_for", lambda owner: {}):
            pass
        r = self.client.post("/mcp/servers/s/oauth/disconnect", json={"scope": "user"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        r = self.client.get("/mcp/servers/s/oauth/status", params={"scope": "user"})
        self.assertEqual(r.status_code, 200)

    def test_callback_error_is_400(self):
        r = self.client.get("/mcp/oauth/finish", params={"error": "denied"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("denied", r.text)

    def test_callback_forwards_to_finish_without_a_session(self):
        r = self.client.get("/mcp/oauth/callback", params={"state": "a b", "code": "x&y", "junk": "1"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("/mcp/oauth/finish?state=a+b&amp;code=x%26y", r.text)
        self.assertNotIn("junk", r.text)

    def test_callback_for_other_account_is_403(self):
        from core import mcp_oauth
        mcp_oauth._flows["st9"] = {"owner": 4242, "name": "s"}
        try:
            r = self.client.get("/mcp/oauth/finish", params={"state": "st9", "code": "c"})
        finally:
            mcp_oauth._flows.pop("st9", None)
        self.assertEqual(r.status_code, 403)


class ManualPasteTests(_McpApp):
    """POST /servers/{name}/oauth/paste: finishing a manual sign-in by pasting the loopback address back."""
    SCFG = {"transport": "http", "url": "http://x"}
    CODE = "SECRET-CODE-123"

    def setUp(self):
        super().setUp()
        self.audit = []
        oauth_endpoints.audit_log = lambda *a, **k: self.audit.append(k)   # restored by the patcher in tearDown
        self.complete_calls, self.connect_calls = [], []

        async def complete(state, code, redirect_uri=None, expect_owner=None, check_owner=False):
            self.complete_calls.append((state, code, redirect_uri, expect_owner, check_owner))
            mcp_oauth._flows.pop(state, None)
            return {"name": "s", "owner": expect_owner}

        async def connect_one(name, cfg, owner=None):
            self.connect_calls.append((name, owner))
            return {"status": "ready", "error": None}
        self.ps = [mock.patch.object(oauth_endpoints, "_servers_for", lambda owner: {"s": self.SCFG}),
                   mock.patch.object(oauth_endpoints, "_redirect_base", lambda: ""),
                   mock.patch.object(oauth_endpoints.mcp_oauth, "begin", _fake_begin()),
                   mock.patch.object(oauth_endpoints.mcp_oauth, "complete", complete),
                   mock.patch.object(oauth_endpoints.mcp_core, "connect_one", connect_one)]
        for p in self.ps:
            p.start()

    def tearDown(self):
        for p in self.ps:
            p.stop()
        oauth_endpoints._manual.clear()
        mcp_oauth._flows.clear()
        super().tearDown()

    def _start(self):
        r = self.client.post("/mcp/servers/s/oauth/start", json={"scope": "user"})
        self.assertEqual(r.json()["mode"], "manual", r.text)
        return r.json()["state"]

    def _paste(self, pasted, **extra):
        return self.client.post("/mcp/servers/s/oauth/paste", json={"scope": "user", "pasted": pasted, **extra})

    def test_full_url_completes(self):
        st = self._start()
        r = self._paste(f"http://127.0.0.1:40123/callback?code={self.CODE}&state={st}&iss=x")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["ok"])
        self.assertEqual(self.complete_calls, [(st, self.CODE, "http://127.0.0.1:40123/callback", 7, True)])
        self.assertEqual(self.connect_calls, [("s", 7)])

    def test_fragment_form_is_accepted(self):
        st = self._start()
        r = self._paste(f"http://127.0.0.1:40123/callback#code={self.CODE}&state={st}")
        self.assertEqual(r.status_code, 200, r.text)

    def test_bare_code_with_state_and_without(self):
        st = self._start()
        self.assertEqual(self._paste(self.CODE, state=st).status_code, 200)
        st = self._start()
        self.assertEqual(self._paste(self.CODE).status_code, 200, "the single pending flow is used")
        self.assertEqual(self.complete_calls[-1][:2], (st, self.CODE))

    def test_wrong_state_rejected_and_flow_survives(self):
        st = self._start()
        r = self._paste(f"http://127.0.0.1:40123/callback?code={self.CODE}&state=nope")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.complete_calls, [])
        self.assertIn(st, oauth_endpoints._manual)

    def test_mismatching_url_state_and_body_state_rejected(self):
        st = self._start()
        r = self._paste(f"http://127.0.0.1:1/callback?code=c&state={st}", state="other")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.complete_calls, [])

    def test_another_users_state_rejected(self):
        st = self._start()
        oauth_endpoints._manual[st]["user"] = 999       # flow was started by someone else
        r = self._paste(f"http://127.0.0.1:40123/callback?code=c&state={st}")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.complete_calls, [])

    def test_another_owner_scope_rejected(self):
        st = self._start()
        oauth_endpoints._manual[st]["owner"] = None     # a global flow cannot be finished from the personal scope
        r = self._paste(f"http://127.0.0.1:40123/callback?code=c&state={st}")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.complete_calls, [])

    def test_expired_state_rejected(self):
        st = self._start()
        mcp_oauth._flows[st]["expires"] = time.time() - 1
        r = self._paste(f"http://127.0.0.1:40123/callback?code=c&state={st}")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.complete_calls, [])
        self.assertNotIn(st, oauth_endpoints._manual)

    def test_reused_state_rejected(self):
        st = self._start()
        url = f"http://127.0.0.1:40123/callback?code=c&state={st}"
        self.assertEqual(self._paste(url).status_code, 200)
        self.assertEqual(self._paste(url).status_code, 400)
        self.assertEqual(len(self.complete_calls), 1)

    def test_oversize_rejected(self):
        self._start()
        self.assertEqual(self._paste("x" * 4097).status_code, 413)
        self.assertEqual(self._paste("c", state="s" * 300).status_code, 413)
        self.assertEqual(self.complete_calls, [])

    def test_provider_error_in_the_address_is_reported_not_completed(self):
        st = self._start()
        r = self._paste(f"http://127.0.0.1:40123/callback?error=access_denied&state={st}")
        self.assertEqual(r.status_code, 400)
        self.assertIn("access_denied", r.json()["error"])
        self.assertEqual(self.complete_calls, [])

    def test_no_code_is_rejected(self):
        self._start()
        self.assertEqual(self._paste("http://127.0.0.1:40123/callback?foo=1").status_code, 400)

    def test_token_exchange_failure_is_reported_and_status_is_error(self):
        st = self._start()

        async def bad(*a, **k):
            raise RuntimeError("token endpoint refused: invalid_grant")
        with mock.patch.object(oauth_endpoints.mcp_oauth, "complete", bad):
            r = self._paste(f"http://127.0.0.1:40123/callback?code=c&state={st}")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(mcp_oauth.get_status("s", 7)["state"], "error")
        self.assertNotIn(st, oauth_endpoints._manual)

    def test_code_never_reaches_audit_or_output(self):
        st = self._start()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            r = self._paste(f"http://127.0.0.1:40123/callback?code={self.CODE}&state={st}")
            self._paste(f"http://127.0.0.1:40123/callback?code={self.CODE}&state={st}")   # reused: error path too
        self.assertEqual(r.status_code, 200)
        blob = out.getvalue() + err.getvalue() + repr(self.audit) + r.text
        self.assertNotIn(self.CODE, blob)
        self.assertNotIn(st, repr(self.audit))
        self.assertEqual(self.audit[-1]["action"], "mcp.oauth.paste")

    def test_permission_failures_are_403(self):
        self.perms = {"chat.use"}
        r = self.client.post("/mcp/servers/s/oauth/paste", json={"scope": "global", "pasted": "c"})
        self.assertEqual(r.status_code, 403)
        self.perms = {"some.other"}
        r = self.client.post("/mcp/servers/s/oauth/paste", json={"scope": "user", "pasted": "c"})
        self.assertEqual(r.status_code, 403)

    def test_unknown_server_404(self):
        with mock.patch.object(oauth_endpoints, "_servers_for", lambda owner: {}):
            self.assertEqual(self._paste("c").status_code, 404)


class ExplainConnectErrorTests(unittest.TestCase):
    CFG = {"url": "https://mcp.example.com/v2/mcp?token=SECRET"}

    def _msg(self, exc):
        from core.mcp.diagnose import explain_connect_error
        return explain_connect_error(exc, self.CFG)

    def test_reasons_are_actionable_and_leak_nothing(self):
        import httpx
        from core.mcp_oauth import McpAuthRequired
        from core.net_guard import BlockedURLError
        cases = {
            "DNS": BlockedURLError("cannot resolve host 'mcp.example.com': [Errno -2]"),
            "Timed out": httpx.ConnectTimeout("t"),
            "TLS": httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED]"),
            "blocked": httpx.ConnectError("refused"),
            "sign in": McpAuthRequired("x"),
            "HTTP 403": RuntimeError("mcp http 403"),
            "HTTP 404": RuntimeError("mcp http 404"),
            "legacy SSE": RuntimeError("mcp http 405"),
            "not with MCP": ValueError("Expecting value"),
        }
        for needle, exc in cases.items():
            m = self._msg(exc)
            self.assertIsNotNone(m, needle)
            self.assertIn(needle.lower(), m.lower(), (needle, m))
            self.assertNotIn("SECRET", m)
            self.assertIn("mcp.example.com", m)

    def test_unknown_error_falls_back(self):
        self.assertIsNone(self._msg(RuntimeError("something odd")))


if __name__ == "__main__":
    unittest.main()


class RedirectBaseAndClientName(unittest.TestCase):
    def test_a_redirect_base_that_already_ends_in_the_callback_path_is_cleaned(self):
        from routes.mcp_manager import oauth_endpoints as oe
        for given, want in (("https://chat.example.com", "https://chat.example.com"),
                            ("https://chat.example.com/", "https://chat.example.com"),
                            ("https://chat.example.com/mcp/oauth/callback", "https://chat.example.com"),
                            ("https://chat.example.com/mcp/oauth/callback/", "https://chat.example.com"),
                            ("https://chat.example.com/mcp", "https://chat.example.com"),
                            ("", "")):
            with mock.patch.dict(oe.APP_CONFIG, {"capabilities": {"mcp_oauth_redirect_base": given}}):
                self.assertEqual(oe._redirect_base(), want, given)

    def test_the_consent_page_name_comes_from_config_and_defaults_to_ssl_ai(self):
        from core.mcp_oauth import flow
        with mock.patch.dict(flow.__dict__.get("APP_CONFIG", {}) or {}, {}):
            pass
        from core.small_model import APP_CONFIG
        with mock.patch.dict(APP_CONFIG, {"capabilities": {}}):
            self.assertEqual(flow.client_name(), "SSL AI")
        with mock.patch.dict(APP_CONFIG, {"capabilities": {"mcp_oauth_client_name": "Acme Assistant"}}):
            self.assertEqual(flow.client_name(), "Acme Assistant")
        self.assertNotIn("A770", flow.client_name())
