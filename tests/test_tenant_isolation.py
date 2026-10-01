"""tests/test_tenant_isolation.py - D2: per-tenant scoping of shared surfaces.

1. GET /db/list works for a database.manage holder (regression: browse.py
   called _discover_workspace_dbs() with no user, so the endpoint 500d).
2. Every db-explorer surface 403s for a chat.use-only user: database.manage
   is documented admin-only, and raw SQL must never be reachable by tenants.
3. One user's /control/switch (cloud branch) records only THEIR model hint;
   the Settings drawer fallback for any other user is untouched.
4. The local branch of /control/switch still demands model.local.load.

Run: python -m unittest tests.test_tenant_isolation -v
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


def _principal(uid, perms, super_admin=False):
    return Principal(id=uid, username=f"u{uid}", display_name=f"u{uid}", is_super_admin=super_admin,
                     must_change_password=False, role_names=[], permission_keys=set(perms))


class _DbApp(unittest.TestCase):
    def _app_for(self, principal):
        from routes import db_explorer
        app = FastAPI()
        app.include_router(db_explorer.router)
        app.dependency_overrides[deps.get_current_user] = lambda: principal
        return app


class DbExplorerPostureTests(_DbApp):
    def test_list_works_for_database_manage_holder(self):
        """Regression: /db/list 500d (TypeError) before the call passed user.id."""
        from routes.db_explorer.endpoints import browse
        with mock.patch.object(browse, "audit_log", lambda *a, **k: None):
            client = TestClient(self._app_for(_principal(7, {"database.manage"})), raise_server_exceptions=False)
            try:
                r = client.get("/db/list")
            finally:
                client.close()
        self.assertEqual(r.status_code, 200, r.text[:300])
        self.assertIn("databases", r.json())

    def test_every_surface_403s_for_chat_use_only(self):
        from routes.db_explorer.endpoints import browse, query
        patches = [mock.patch.object(m, "audit_log", lambda *a, **k: None) for m in (browse, query)]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        client = TestClient(self._app_for(_principal(9, {"chat.use"})), raise_server_exceptions=False)
        self.addCleanup(client.close)
        self.assertEqual(client.get("/db/list").status_code, 403)
        self.assertEqual(client.get("/db/projects/schema").status_code, 403)
        self.assertEqual(client.get("/db/projects/table/sessions/data").status_code, 403)
        r = client.post("/db/projects/query", json={"sql": "SELECT * FROM sessions", "max_rows": 10})
        self.assertEqual(r.status_code, 403, r.text[:200])


class ModelHintIsolationTests(unittest.TestCase):
    def setUp(self):
        from routes.control import config_endpoints, server_endpoints  # noqa: F401 (register routes)
        from routes.control.base import router
        from routes import common
        self.common = common
        common._model_hints.clear()
        self.holder = {"principal": _principal(11, {"chat.use"})}
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[deps.get_current_user] = lambda: self.holder["principal"]
        self.patches = [
            mock.patch.object(server_endpoints, "audit_log", lambda *a, **k: None),
            mock.patch.object(server_endpoints.cloud, "set_lanes", lambda *a, **k: {}),
        ]
        for p in self.patches:
            p.start()
        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self):
        self.client.close()
        for p in self.patches:
            p.stop()
        self.common._model_hints.clear()

    def _fake_cm(self, key="openai/gpt-4o-mini"):
        cm = mock.Mock()
        cm.key = key
        cm.display = "GPT-4o mini"
        cm.provider_name = "openai"
        cm.endpoint.return_value = "https://api.openai.com/v1"
        return cm

    def test_cloud_switch_records_only_the_callers_hint(self):
        from routes.control import server_endpoints
        alice, bob = _principal(11, {"chat.use"}), _principal(12, {"chat.use"})
        with mock.patch.object(server_endpoints.cloud, "get_cloud", return_value=self._fake_cm()):
            self.holder["principal"] = alice
            r = self.client.post("/control/switch", json={"target": "cloud:openai/gpt-4o-mini"})
            self.assertEqual(r.status_code, 200, r.text[:200])
        self.assertEqual(self.common.get_model_hint(alice.id), "cloud:openai/gpt-4o-mini")
        self.assertIsNone(self.common.get_model_hint(bob.id))

    def test_local_branch_still_requires_model_local_load(self):
        self.holder["principal"] = _principal(13, {"chat.use"})
        r = self.client.post("/control/switch", json={"target": "C:\\models\\qwen.gguf"})
        self.assertEqual(r.status_code, 403, r.text[:200])
        self.assertIsNone(self.common.get_model_hint(13))


if __name__ == "__main__":
    unittest.main()
