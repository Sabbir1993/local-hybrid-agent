"""tests/test_oauth_device_flow.py - E3: OAuth device-flow helper (was 0% covered).

LIVE code, not dead: routes/mcp_manager/catalog_endpoints.py drives it for
connector authorization (start/new_session/get_session/drop_session/poll).
Zero coverage only because it needs a provider network round-trip -- these
tests fake the httpx transport, so no network is touched.

Run: python -m unittest tests.test_oauth_device_flow -v
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import oauth_device_flow as df


class _Resp:
    def __init__(self, payload=None, status=200, json_raises=False):
        self._payload = payload or {}
        self.status_code = status
        self._json_raises = json_raises

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("bad", request=None, response=None)

    def json(self):
        if self._json_raises:
            raise ValueError("not json")
        return self._payload


class _Client:
    """Async context manager standing in for httpx.AsyncClient."""
    posted = None

    def __init__(self, resp=None, exc=None):
        self._resp = resp or _Resp({})
        self._exc = exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, data=None, headers=None):
        _Client.posted = {"url": url, "data": dict(data or {})}
        if self._exc:
            raise self._exc
        return self._resp


def _client(resp=None, exc=None):
    return mock.patch.object(df.httpx, "AsyncClient", lambda *a, **k: _Client(resp, exc))


class StartTests(unittest.TestCase):
    CFG = {"device_code_url": "https://x/device", "client_id": "cid", "scope": "s"}

    def test_maps_rfc_8628_response(self):
        import asyncio
        payload = {"device_code": "dc", "user_code": "UC-12",
                   "verification_uri": "https://x/verify",
                   "expires_in": 600, "interval": 7}
        with _client(_Resp(payload)):
            out = asyncio.run(df.start(self.CFG))
        self.assertEqual(out, {"device_code": "dc", "user_code": "UC-12",
                               "verification_uri": "https://x/verify",
                               "expires_in": 600, "interval": 7})
        self.assertEqual(_Client.posted["data"]["client_id"], "cid")
        self.assertEqual(_Client.posted["url"], "https://x/device")

    def test_fallbacks(self):
        import asyncio
        payload = {"device_code": "dc", "user_code": "UC-12",
                   "verification_uri_complete": "https://x/all?c=1"}
        with _client(_Resp(payload)):
            out = asyncio.run(df.start(self.CFG))
        self.assertEqual(out["verification_uri"], "https://x/all?c=1")
        self.assertEqual((out["expires_in"], out["interval"]), (900, 5))

    def test_http_error_propagates_for_the_route_502(self):
        import asyncio
        import httpx
        with _client(_Resp({}, status=500)):
            with self.assertRaises(httpx.HTTPStatusError):
                asyncio.run(df.start(self.CFG))


class PollTests(unittest.TestCase):
    CFG = {"token_url": "https://x/token", "client_id": "cid"}

    def _poll(self, payload=None, status=200, json_raises=False):
        import asyncio
        with _client(_Resp(payload, status, json_raises)):
            return asyncio.run(df.poll(self.CFG, "dc"))

    def test_success(self):
        r = self._poll({"access_token": "tok"})
        self.assertEqual(r, {"status": "success", "token": "tok"})

    def test_pending_and_slow_down(self):
        self.assertEqual(self._poll({"error": "authorization_pending"})["status"], "pending")
        r = self._poll({"error": "slow_down"})
        self.assertEqual((r["status"], r["slow_down"]), ("pending", True))

    def test_terminal_states(self):
        self.assertEqual(self._poll({"error": "expired_token"})["status"], "expired")
        self.assertEqual(self._poll({"error": "access_denied"})["status"], "denied")

    def test_error_paths(self):
        self.assertEqual(self._poll({"error": "weird"})["status"], "error")
        self.assertEqual(self._poll({})["status"], "error")
        r = self._poll(status=500, json_raises=True)
        self.assertEqual(r["status"], "error")
        self.assertIn("500", r["error"])


class SessionTests(unittest.TestCase):
    def test_lifecycle(self):
        sid = df.new_session("srv", "dc", 5, 900)
        got = df.get_session(sid)
        self.assertEqual((got["server_id"], got["device_code"]), ("srv", "dc"))
        df.drop_session(sid)
        self.assertIsNone(df.get_session(sid))
        self.assertIsNone(df.get_session("nope"))

    def test_expiry(self):
        sid = df.new_session("srv", "dc", 5, -1)  # already past
        self.assertIsNone(df.get_session(sid))
        self.assertNotIn(sid, df._sessions)  # reaped, not leaked


class RouteWiringTests(unittest.TestCase):
    """The catalog authorize endpoints over a faked device flow (no network)."""

    def setUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from core import deps
        from core.auth import Principal
        from routes.mcp_manager.base import router
        from routes.mcp_manager import catalog_endpoints
        app = FastAPI()
        app.include_router(router)
        me = Principal(id=7, username="u7", display_name="u7", is_super_admin=False,
                       must_change_password=False, role_names=[],
                       permission_keys={"settings.orchestration.configure"})
        app.dependency_overrides[deps.get_current_user] = lambda: me
        self._audit = mock.patch.object(catalog_endpoints, "audit_log", lambda *a, **k: None)
        self._audit.start()
        self.addCleanup(self._audit.stop)
        self.entry = {"id": "gh", "auth": {"type": "device_flow", "client_id": "cid",
                                           "device_code_url": "https://x/d",
                                           "token_url": "https://x/t"}}
        self._get = mock.patch.object(catalog_endpoints.mcp_catalog, "get_entry",
                                      lambda sid: self.entry if sid == "gh" else None)
        self._get.start()
        self.addCleanup(self._get.stop)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def test_authorize_start_success(self):
        import asyncio
        payload = {"device_code": "dc", "user_code": "UC-1",
                   "verification_uri": "https://x/v", "expires_in": 900, "interval": 5}
        with _client(_Resp(payload)):
            r = self.client.post("/mcp/gh/authorize/start")
        self.assertEqual(r.status_code, 200, r.text[:200])
        body = r.json()
        self.assertEqual(body["user_code"], "UC-1")
        self.assertIn("session", body)
        # the session is real: polling it reaches the (faked) token endpoint
        with _client(_Resp({"error": "authorization_pending"})):
            r = self.client.get("/mcp/gh/authorize/poll", params={"session": body["session"]})
        self.assertEqual(r.json()["status"], "pending")

    def test_poll_unknown_session_is_expired(self):
        r = self.client.get("/mcp/gh/authorize/poll", params={"session": "ghost"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "expired")


if __name__ == "__main__":
    unittest.main()
