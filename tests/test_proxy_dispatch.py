"""tests/test_proxy_dispatch.py - E2: proxy endpoint dispatch (previously ~15% covered).

The handler is unreachable without a model, but its guard rails must hold
anyway: path allow-list, input-sanitizer blocks, cloud quota, and the
no-model/model-not-permitted 503s. No GPU, no server binary needed.

Run: python -m unittest tests.test_proxy_dispatch -v
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import deps
from core.auth import Principal
from routes.proxy import endpoints as proxy_endpoints


def _principal(perms):
    return Principal(id=7, username="u7", display_name="u7", is_super_admin=False,
                     must_change_password=False, role_names=[], permission_keys=set(perms))


async def _guard_allow(*a, **k):
    return None


class _ProxyApp(unittest.TestCase):
    perms = {"chat.use"}

    def setUp(self):
        app = FastAPI()
        app.include_router(proxy_endpoints.router)
        app.dependency_overrides[deps.get_current_user] = lambda: _principal(self.perms)
        self.patches = [
            mock.patch.object(proxy_endpoints, "audit_log", lambda *a, **k: None),
            mock.patch.object(proxy_endpoints.input_guard, "check_async", _guard_allow),
        ]
        for p in self.patches:
            p.start()
        # no model running, nothing selected: exercises the guard rails only
        fake_state = SimpleNamespace(process=None, client=None, profile_path=None,
                                     profile=None, last_activity=0.0)
        self._state = mock.patch.object(proxy_endpoints, "state", fake_state)
        self._state.start()
        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self):
        self.client.close()
        self._state.stop()
        for p in self.patches:
            p.stop()


class DispatchTests(_ProxyApp):
    def test_unknown_path_404s_without_touching_a_model(self):
        r = self.client.get("/slots")
        self.assertEqual(r.status_code, 404)

    def test_needs_chat_use(self):
        self.perms = set()
        r = self.client.get("/v1/models")
        self.assertEqual(r.status_code, 403)

    def test_input_guard_block_is_403(self):
        async def blocked(*a, **k):
            return {"name": "rule", "message": "blocked", "scope": "proxy",
                    "_matched_pattern": "p"}
        with mock.patch.object(proxy_endpoints.input_guard, "check_async", blocked):
            r = self.client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["error"]["type"], "input_guard_block")

    def test_cloud_quota_is_429_before_any_upstream_call(self):
        with mock.patch.object(proxy_endpoints.cloud, "cloud_lane", lambda *a, **k: object()), \
             mock.patch.object(proxy_endpoints, "check_cloud_request_quota",
                               lambda uid: "daily cap hit"), \
             mock.patch.object(proxy_endpoints, "_proxy_cloud") as pc:
            r = self.client.post("/v1/chat/completions", json={"messages": []})
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.json()["error"]["type"], "cloud_quota_exceeded")
        pc.assert_not_called()

    def test_no_model_no_target_is_503(self):
        proxy_endpoints.state.profile_path = None
        proxy_endpoints.state.profile = None
        r = self.client.get("/v1/models")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["error"]["type"], "model_not_loaded")

    def test_no_model_with_target_needs_load_permission(self):
        proxy_endpoints.state.profile_path = "/models/qwen.gguf"
        r = self.client.get("/v1/models")
        self.assertEqual(r.status_code, 503)
        self.assertIn("lack permission", r.json()["error"]["message"])


if __name__ == "__main__":
    unittest.main()
