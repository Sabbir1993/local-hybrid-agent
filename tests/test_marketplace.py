"""tests/test_marketplace.py - Remote plugin marketplace (routes/customize.py).

Covers: GET /customize/plugins/registry, POST /customize/plugins/inspect-remote,
POST /customize/plugins/install-remote. Network is mocked (no real fetches).

Run: python -m unittest tests.test_marketplace -v
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
from core import plugins as pc
from core.auth import Principal
from core.net_guard import FetchResult
from core.small_model import APP_CONFIG
from routes import customize as cz


def _principal(perms):
    return Principal(id=1, username="t", display_name="t", is_super_admin=False,
                     must_change_password=False, role_names=[], permission_keys=set(perms))


MANIFEST = {"name": "remotedemo", "title": "Remote Demo", "description": "d",
            "version": "1.0", "author": "a", "category": "developer",
            "tools": [], "code_url": "https://example.com/p.py"}
CODE = b"def register(api):\n    api.add_system_prompt('hi')\n"
REG = json.dumps({"plugins": [{"name": "remotedemo", "title": "Remote Demo",
        "manifest_url": "https://example.com/m.json",
        "code_url": "https://example.com/p.py"}]}).encode()


def _fake_guarded(url, timeout, headers=None, max_bytes=100):
    if "registry" in url:
        return FetchResult(url=url, status_code=200, headers={}, content=REG)
    if url.endswith("m.json"):
        return FetchResult(url=url, status_code=200, headers={},
                           content=json.dumps(MANIFEST).encode())
    if url.endswith("p.py"):
        return FetchResult(url=url, status_code=200, headers={}, content=CODE)
    return FetchResult(url=url, status_code=404, headers={}, content=b"")


class MarketplaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.saved_caps = copy.deepcopy(APP_CONFIG.get("capabilities", {}))
        APP_CONFIG.setdefault("capabilities", {})["plugins"] = True
        APP_CONFIG["capabilities"]["plugins_disabled"] = []
        APP_CONFIG["capabilities"]["plugin_marketplace_url"] = "https://example.com/registry.json"
        self.patches = [
            mock.patch.object(pc, "CATALOG_DIR", root / "plugin_catalog"),
            mock.patch.object(pc, "PLUGINS_DIR", root / "plugins"),
            mock.patch.object(cz, "audit_log", lambda *a, **k: None),
            mock.patch.object(deps, "audit_log", lambda *a, **k: None),
            mock.patch("routes.customize.guarded_get", side_effect=_fake_guarded),
        ]
        for p in self.patches:
            p.start()
        app = FastAPI()
        app.include_router(cz.router)
        self.perms = {"capabilities.install"}
        app.dependency_overrides[deps.get_current_user] = lambda: _principal(self.perms)
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        pc.unload_all()
        pc._errors.clear()
        for p in self.patches:
            p.stop()
        APP_CONFIG["capabilities"] = self.saved_caps
        self.tmp.cleanup()

    def test_registry_lists_entries(self):
        d = self.client.get("/customize/plugins/registry").json()
        self.assertEqual(d["count"], 1)
        self.assertEqual(d["items"][0]["manifest_url"], "https://example.com/m.json")

    def test_registry_blocks_private_override(self):
        r = self.client.get("/customize/plugins/registry",
                            params={"url": "http://127.0.0.1/evil.json"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("blocked", r.json()["error"])

    def test_inspect_returns_preview_without_writing(self):
        d = self.client.post("/customize/plugins/inspect-remote",
                             json={"manifest_url": "https://example.com/m.json"}).json()
        self.assertEqual(d["manifest"]["name"], "remotedemo")
        self.assertEqual(d["code_syntax"], "ok")
        self.assertIn("add_system_prompt", d["code_preview"])
        self.assertFalse(d["name_taken"])

    def test_inspect_blocks_private_url(self):
        r = self.client.post("/customize/plugins/inspect-remote",
                             json={"manifest_url": "http://169.254.169.254/x"})
        self.assertEqual(r.status_code, 400)

    def test_install_remote_writes_and_loads(self):
        r = self.client.post("/customize/plugins/install-remote",
                             json={"manifest_url": "https://example.com/m.json"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["loaded"])
        d = self.client.post("/customize/plugins/inspect-remote",
                             json={"manifest_url": "https://example.com/m.json"}).json()
        self.assertTrue(d["name_taken"])

    def test_install_remote_needs_permission(self):
        self.perms.clear()
        r = self.client.post("/customize/plugins/install-remote",
                             json={"manifest_url": "https://example.com/m.json"})
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
