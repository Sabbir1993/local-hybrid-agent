"""tests/test_mcp_oauth.py - OAuth for remote HTTP MCP servers (core/mcp_oauth.py) and
its wiring into core/mcp.py + routes/mcp_manager.py. No network: discovery and token
endpoints are mocked, the OS keychain is an in-memory dict.

Run: python -m pytest tests/test_mcp_oauth.py -q
"""

import asyncio
import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import mcp as mcp_core
from core import mcp_oauth

GMAIL_URL = "https://gmailmcp.googleapis.com/mcp/v1"
GOOGLE_META = {"issuer": "https://accounts.google.com/", "resource": GMAIL_URL,
               "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
               "token_endpoint": "https://oauth2.googleapis.com/token", "scopes_supported": []}


class _Keychain:
    def __init__(self):
        self.d = {}

    def set_token(self, k, v):
        self.d[k] = v

    def get_token(self, k):
        return self.d.get(k)

    def delete_token(self, k):
        self.d.pop(k, None)

    def has_token(self, k):
        return k in self.d


def run(coro):
    return asyncio.run(coro)


class OAuthFlowTests(unittest.TestCase):
    def setUp(self):
        self.kc = _Keychain()
        self._p = [mock.patch("core.credentials." + n, getattr(self.kc, n))
                   for n in ("set_token", "get_token", "delete_token", "has_token")]
        for p in self._p:
            p.start()
        self._g = mock.patch.object(mcp_oauth, "_guard", lambda url, owner: None)
        self._g.start()
        mcp_oauth._flows.clear()

    def tearDown(self):
        for p in self._p:
            p.stop()
        self._g.stop()

    def _cfg(self, **auth):
        return {"transport": "http", "url": GMAIL_URL,
                "auth": {"type": "oauth", "client_id": "cid.apps.googleusercontent.com",
                         "scopes": ["https://www.googleapis.com/auth/gmail.readonly"], **auth}}

    def test_begin_builds_pkce_url_with_placeholder_for_google(self):
        with mock.patch.object(mcp_oauth, "discover", mock.AsyncMock(return_value=GOOGLE_META)):
            start = run(mcp_oauth.begin("gmail", 1, self._cfg()))
        q = parse_qs(urlparse(start["auth_url"]).query)
        self.assertEqual(q["redirect_uri"], [mcp_oauth.REDIRECT_PLACEHOLDER])
        self.assertEqual(q["code_challenge_method"], ["S256"])
        self.assertEqual(q["access_type"], ["offline"])
        self.assertNotIn("resource", q)                 # Google rejects unknown params
        self.assertEqual(q["state"], [start["state"]])
        self.assertIn(start["state"], mcp_oauth._flows)

    def test_complete_stores_token_in_keychain_and_refresh_keeps_refresh_token(self):
        self.kc.set_token(mcp_core.secret_env_ref("gmail", mcp_oauth.SECRET_KEY, 1), "shh")
        with mock.patch.object(mcp_oauth, "discover", mock.AsyncMock(return_value=GOOGLE_META)):
            start = run(mcp_oauth.begin("gmail", 1, self._cfg()))
        tok_resp = {"access_token": "AT1", "refresh_token": "RT1", "expires_in": 3600, "token_type": "Bearer"}
        with mock.patch.object(mcp_oauth, "_token_request", mock.AsyncMock(return_value=tok_resp)) as tr:
            done = run(mcp_oauth.complete(start["state"], "the-code", "http://127.0.0.1:5555/callback"))
        sent = tr.call_args.args[1]
        self.assertEqual(sent["client_secret"], "shh")
        self.assertEqual(sent["redirect_uri"], "http://127.0.0.1:5555/callback")
        self.assertTrue(sent["code_verifier"])
        self.assertEqual(done, {"name": "gmail", "owner": 1})
        self.assertEqual(run(mcp_oauth.access_token("gmail", 1)), "AT1")
        # the state is single-use
        with self.assertRaises(RuntimeError):
            run(mcp_oauth.complete(start["state"], "again"))

        # expire it: refresh, provider doesn't rotate the refresh token
        tok = mcp_oauth.load_token("gmail", 1)
        tok["expires_at"] = time.time() + 5
        mcp_oauth.save_token("gmail", 1, tok)
        with mock.patch.object(mcp_oauth, "_token_request",
                               mock.AsyncMock(return_value={"access_token": "AT2", "expires_in": 3600})):
            self.assertEqual(run(mcp_oauth.access_token("gmail", 1)), "AT2")
        self.assertEqual(mcp_oauth.load_token("gmail", 1)["refresh_token"], "RT1")

    def test_invalid_grant_clears_token(self):
        mcp_oauth.save_token("gmail", 1, {"access_token": "old", "refresh_token": "RT", "expires_at": time.time() - 1,
                                          "token_endpoint": "https://oauth2.googleapis.com/token", "client_id": "c"})
        with mock.patch.object(mcp_oauth, "_token_request",
                               mock.AsyncMock(side_effect=RuntimeError("token endpoint refused: invalid_grant"))):
            self.assertIsNone(run(mcp_oauth.access_token("gmail", 1)))
        self.assertFalse(mcp_oauth.has_token("gmail", 1))

    def test_tokens_are_per_user(self):
        mcp_oauth.save_token("gmail", 1, {"access_token": "u1"})
        self.assertIsNone(mcp_oauth.load_token("gmail", 2))
        self.assertIsNone(mcp_oauth.load_token("gmail", None))

    def test_dynamic_client_registration_when_no_client_id(self):
        meta = {**GOOGLE_META, "resource": "https://mcp.example.com/mcp",
                "authorization_endpoint": "https://as.example.com/authorize",
                "token_endpoint": "https://as.example.com/token",
                "registration_endpoint": "https://as.example.com/register"}
        cfg = {"transport": "http", "url": "https://mcp.example.com/mcp", "auth": {"type": "oauth"}}
        reg = mock.AsyncMock(return_value={"client_id": "dyn-123",
                                           "redirect_uri": f"http://127.0.0.1:{mcp_oauth.DEFAULT_DCR_PORT}/callback"})
        with mock.patch.object(mcp_oauth, "discover", mock.AsyncMock(return_value=meta)), \
                mock.patch.object(mcp_oauth, "_register_client", reg):
            start = run(mcp_oauth.begin("ex", 1, cfg))
        q = parse_qs(urlparse(start["auth_url"]).query)
        self.assertEqual(q["client_id"], ["dyn-123"])
        self.assertEqual(start["loopback_port"], mcp_oauth.DEFAULT_DCR_PORT)
        self.assertEqual(q["resource"], ["https://mcp.example.com/mcp"])   # RFC 8707 for non-Google

    def test_begin_uses_the_admins_shared_client_for_the_real_connector(self):
        self.kc.d["oauthapp:gmail:secret"] = "shared-secret"
        cfg = {"transport": "http", "url": GMAIL_URL, "auth": {"type": "oauth"}}     # no client id of its own
        with mock.patch("core.mcp_catalog.shared_client_id", return_value="shared-cid"), \
                mock.patch.object(mcp_oauth, "discover", mock.AsyncMock(return_value=GOOGLE_META)):
            start = run(mcp_oauth.begin("gmail", 7, cfg))
        self.assertEqual(parse_qs(urlparse(start["auth_url"]).query)["client_id"], ["shared-cid"])
        flow = mcp_oauth._flows[start["state"]]
        self.assertEqual(flow["client_secret"], "shared-secret")
        self.assertTrue(flow["shared_client"])
        # the stored token remembers it, so a later refresh can use the shared secret
        with mock.patch.object(mcp_oauth, "_token_request",
                               mock.AsyncMock(return_value={"access_token": "AT", "refresh_token": "RT", "expires_in": 3600})):
            run(mcp_oauth.complete(start["state"], "the-code", "http://127.0.0.1:1/callback"))
        self.assertTrue(mcp_oauth.load_token("gmail", 7)["shared_client"])
        tok = mcp_oauth.load_token("gmail", 7)
        tok["expires_at"] = time.time() - 5
        mcp_oauth.save_token("gmail", 7, tok)
        seen = {}

        async def fake(url, data, owner):
            seen.update(data)
            return {"access_token": "AT2", "expires_in": 3600}
        with mock.patch.object(mcp_oauth, "_token_request", fake):
            run(mcp_oauth.access_token("gmail", 7))
        self.assertEqual(seen["client_secret"], "shared-secret")
        self.assertTrue(mcp_oauth.load_token("gmail", 7)["shared_client"])

    def test_a_same_named_personal_server_elsewhere_never_gets_the_shared_secret(self):
        self.kc.d["oauthapp:gmail:secret"] = "shared-secret"
        evil = {"transport": "http", "url": "https://attacker.example/mcp", "auth": {"type": "oauth"}}
        meta = {**GOOGLE_META, "resource": "https://attacker.example/mcp",
                "authorization_endpoint": "https://as.attacker.example/authorize",
                "token_endpoint": "https://as.attacker.example/token"}
        with mock.patch("core.mcp_catalog.shared_client_id", return_value="shared-cid"), \
                mock.patch.object(mcp_oauth, "discover", mock.AsyncMock(return_value=meta)):
            with self.assertRaises(RuntimeError):          # no client id, no registration: refuses rather than borrowing
                run(mcp_oauth.begin("gmail", 9, evil))

    def test_migrates_plaintext_client_credentials(self):
        cfg = {"transport": "http", "url": GMAIL_URL, "env": {"clientId": "cid", "clientSecret": "s3cret", "X": "1"}}
        new = mcp_oauth.migrate_plain_client("gmail", 1, cfg)
        self.assertEqual(new["auth"]["client_id"], "cid")
        self.assertEqual(new["env"], {"X": "1"})
        self.assertIn(mcp_oauth.SECRET_KEY, new["secret_env_keys"])
        self.assertIn("https://www.googleapis.com/auth/gmail.readonly", new["auth"]["scopes"])
        self.assertEqual(self.kc.get_token(mcp_core.secret_env_ref("gmail", mcp_oauth.SECRET_KEY, 1)), "s3cret")
        self.assertNotIn("s3cret", json.dumps(new))
        self.assertIsNone(mcp_oauth.migrate_plain_client("gmail", 1, new))   # idempotent


class _Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body or {}
        self.headers = {"content-type": "application/json", **(headers or {})}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class McpHttpAuthTests(unittest.TestCase):
    def test_401_forces_refresh_then_retries_once(self):
        srv = mcp_core.McpServer("gmail", {"transport": "http", "url": GMAIL_URL}, owner=None)
        calls = []

        class FakeClient:
            async def post(self, url, json=None, headers=None, timeout=None):
                calls.append(headers.get("Authorization"))
                if headers.get("Authorization") == "Bearer old":
                    return _Resp(401)
                return _Resp(200, {"jsonrpc": "2.0", "id": json["id"], "result": {"ok": True}})

        srv._http = FakeClient()
        # first request, forced refresh, then the retried request
        with mock.patch.object(mcp_oauth, "access_token", mock.AsyncMock(side_effect=["old", "new", "new"])):
            res = run(srv._http_rpc("tools/call", {"name": "x"}))
        self.assertEqual(res, {"ok": True})
        self.assertEqual(calls, ["Bearer old", "Bearer new"])

    def test_401_without_token_asks_user_to_sign_in(self):
        srv = mcp_core.McpServer("gmail", {"transport": "http", "url": GMAIL_URL}, owner=None)

        class FakeClient:
            async def post(self, *a, **k):
                return _Resp(401)

        srv._http = FakeClient()
        with mock.patch.object(mcp_oauth, "access_token", mock.AsyncMock(return_value=None)):
            with self.assertRaises(mcp_oauth.McpAuthRequired):
                run(srv._http_rpc("tools/call", {"name": "x"}))
        self.assertTrue(srv.auth_required)
        self.assertTrue(srv.status_info()["auth_required"])

    def test_tools_list_follows_cursor(self):
        srv = mcp_core.McpServer("x", {"transport": "http", "url": "https://x"}, owner=None)
        pages = {None: {"tools": [{"name": "a"}], "nextCursor": "p2"}, "p2": {"tools": [{"name": "b"}]}}
        srv._rpc = mock.AsyncMock(side_effect=lambda m, p=None, **k: pages[(p or {}).get("cursor")])
        self.assertEqual([t["name"] for t in run(srv._list_tools())], ["a", "b"])

    def test_npx_timeout_through_cmd_wrapper(self):
        srv = mcp_core.McpServer("x", {"command": "cmd", "args": ["/c", "npx", "-y", "pkg"]})
        self.assertEqual(srv._init_timeout(), mcp_core.NPX_INIT_TIMEOUT_S)


class McpManagerAuthValidationTests(unittest.TestCase):
    def _req(self, **kw):
        from routes.mcp_manager import McpServerReq
        base = dict(name="gmail", scope="user", transport="http", url=GMAIL_URL)
        base.update(kw)
        return McpServerReq(**base)

    def test_rejects_secret_in_auth_or_headers(self):
        from routes.mcp_manager import _validate
        with mock.patch("routes.mcp_manager.validation.check_url", lambda u: None):
            self.assertIn("OAUTH_CLIENT_SECRET", _validate(self._req(auth={"type": "oauth", "client_secret": "x"}), True))
            self.assertIn("can't be set", _validate(self._req(headers={"Authorization": "Bearer x"}), True))
            self.assertIsNone(_validate(self._req(auth={"type": "oauth", "client_id": "c", "scopes": ["s"]}), True))

    def test_custom_cfg_keeps_auth_block(self):
        from routes.mcp_manager import _custom_cfg
        cfg = _custom_cfg(self._req(auth={"type": "oauth", "client_id": "c", "scopes": ["a"], "auth_url": ""},
                                    secret_env={"OAUTH_CLIENT_SECRET": "s"}), [])
        self.assertEqual(cfg["auth"], {"type": "oauth", "client_id": "c", "scopes": ["a"]})
        self.assertEqual(cfg["secret_env_keys"], ["OAUTH_CLIENT_SECRET"])
        self.assertNotIn("s", json.dumps(cfg.get("env", {})))


if __name__ == "__main__":
    unittest.main()
