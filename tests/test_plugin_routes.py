"""tests/test_plugin_routes.py - Settings UI plugin catalog: browse / install / enable / uninstall.

Run: python -m unittest tests.test_plugin_routes -v
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
from core.registry import registry
from core.small_model import APP_CONFIG
from routes import plugins as rp

PLUGIN_SRC = '''
def register(api):
    api.add_system_prompt("demo active")
    api.add_tool("ping", lambda args: "pong", "reply pong")
'''


def _principal(perms):
    return Principal(id=1, username="t", display_name="t", is_super_admin=False,
                     must_change_password=False, role_names=[], permission_keys=set(perms))


class PluginRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.catalog = root / "plugin_catalog"
        self.installed = root / "plugins"
        (self.catalog / "demo").mkdir(parents=True)
        (self.catalog / "demo" / "plugin.py").write_text(PLUGIN_SRC, encoding="utf-8")
        (self.catalog / "demo" / "plugin.json").write_text(json.dumps(
            {"title": "Demo", "description": "d", "category": "developer", "tools": ["ping"]}), encoding="utf-8")
        (self.catalog / "broken").mkdir()
        (self.catalog / "broken" / "plugin.py").write_text("def register(api):\n    raise RuntimeError('boom')\n",
                                                           encoding="utf-8")
        self.installed.mkdir()
        self.cfg_file = root / "app.json"
        self.cfg_file.write_text(json.dumps({"capabilities": {"plugins": True}}), encoding="utf-8")

        self.saved_caps = copy.deepcopy(APP_CONFIG.get("capabilities", {}))
        APP_CONFIG.setdefault("capabilities", {})["plugins"] = True
        APP_CONFIG["capabilities"]["plugins_disabled"] = []
        self.patches = [
            mock.patch.object(pc, "CATALOG_DIR", self.catalog),
            mock.patch.object(pc, "PLUGINS_DIR", self.installed),
            mock.patch.object(rp, "CONFIG_FILE", self.cfg_file),
            mock.patch.object(rp, "audit_log", lambda *a, **k: None),
            mock.patch.object(deps, "audit_log", lambda *a, **k: None),
        ]
        for p in self.patches:
            p.start()
        app = FastAPI()
        app.include_router(rp.router)
        self.perms = {"settings.orchestration.configure"}
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

    def _tools(self, name="demo"):
        return [t.name for t in registry.list(source=f"plugin:{name}")]

    def test_catalog_lists_bundles(self):
        d = self.client.get("/plugins/catalog").json()
        names = {p["name"]: p for p in d["plugins"]}
        self.assertEqual(set(names), {"demo", "broken"})
        self.assertEqual(names["demo"]["title"], "Demo")
        self.assertFalse(names["demo"]["installed"])
        self.assertEqual(len(names["demo"]["sha256"]), 64)

    def test_install_copies_and_hot_loads(self):
        r = self.client.post("/plugins/demo/install")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue((self.installed / "demo" / "plugin.py").is_file())
        self.assertEqual(self._tools(), ["plugin__demo__ping"])
        self.assertIn("demo active", pc.plugins_prompt_fragment())
        self.assertTrue(self.client.get("/plugins/catalog").json()["plugins"][1]["installed"])

    def test_install_unknown_or_bad_name_refused(self):
        self.assertEqual(self.client.post("/plugins/nope/install").status_code, 400)
        self.assertEqual(self.client.post("/plugins/..%2Fx/install").status_code in (400, 404), True)
        self.assertEqual(self.client.post("/plugins/_x/install").status_code, 400)

    def test_broken_plugin_reports_error_and_leaves_no_tools(self):
        r = self.client.post("/plugins/broken/install").json()
        self.assertFalse(r["plugin"]["loaded"])
        self.assertIn("boom", r["plugin"]["error"])
        self.assertEqual(self._tools("broken"), [])

    def test_disable_unloads_and_persists(self):
        self.client.post("/plugins/demo/install")
        r = self.client.post("/plugins/demo/enable", json={"enabled": False})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._tools(), [])
        self.assertEqual(json.loads(self.cfg_file.read_text())["capabilities"]["plugins_disabled"], ["demo"])
        # a full reload must respect the disable list
        self.client.post("/plugins/reload")
        self.assertEqual(self._tools(), [])
        self.client.post("/plugins/demo/enable", json={"enabled": True})
        self.assertEqual(self._tools(), ["plugin__demo__ping"])

    def test_uninstall_removes_catalog_plugin(self):
        self.client.post("/plugins/demo/install")
        r = self.client.delete("/plugins/demo")
        self.assertEqual(r.status_code, 200)
        self.assertFalse((self.installed / "demo").exists())
        self.assertEqual(self._tools(), [])

    def test_uninstall_refuses_hand_written_or_modified(self):
        (self.installed / "mine").mkdir()
        (self.installed / "mine" / "plugin.py").write_text(PLUGIN_SRC, encoding="utf-8")
        self.assertEqual(self.client.delete("/plugins/mine").status_code, 400)
        self.assertTrue((self.installed / "mine").exists())

        self.client.post("/plugins/demo/install")
        (self.installed / "demo" / "plugin.py").write_text(PLUGIN_SRC + "\n# edit\n", encoding="utf-8")
        self.assertEqual(self.client.delete("/plugins/demo").status_code, 400)
        self.assertEqual(self.client.post("/plugins/demo/install").status_code, 400)

    def test_mutations_require_permission(self):
        self.perms = set()
        self.assertEqual(self.client.post("/plugins/demo/install").status_code, 403)
        self.assertEqual(self.client.post("/plugins/reload").status_code, 403)
        self.assertFalse((self.installed / "demo").exists())
        self.assertEqual(self.client.get("/plugins/catalog").status_code, 200)


if __name__ == "__main__":
    unittest.main()
