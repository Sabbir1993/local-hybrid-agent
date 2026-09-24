"""tests/test_customize_routes.py - Customize page: browse / install / uninstall across
skills, plugins and connectors.

Run: python -m unittest tests.test_customize_routes -v
"""

import copy
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
from core import skills as sk
from core.auth import Principal
from core.small_model import APP_CONFIG
from routes import customize as cz

SKILL_MD = "---\nname: demo\ndescription: d\ncategory: security\nauthor: t\n---\nbody\n"


def _principal(perms):
    return Principal(id=1, username="t", display_name="t", is_super_admin=False,
                     must_change_password=False, role_names=[], permission_keys=set(perms))


class CustomizeRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.catalog = root / "skill_catalog"
        self.installed = root / "skills"
        (self.catalog / "demo").mkdir(parents=True)
        (self.catalog / "demo" / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
        self.installed.mkdir()
        self.saved_caps = copy.deepcopy(APP_CONFIG.get("capabilities", {}))
        self.patches = [
            mock.patch.object(sk, "CATALOG_DIR", self.catalog),
            mock.patch.object(sk, "SKILLS_DIR", self.installed),
            mock.patch.object(pc, "CATALOG_DIR", root / "plugin_catalog"),
            mock.patch.object(pc, "PLUGINS_DIR", root / "plugins"),
            mock.patch.object(cz, "audit_log", lambda *a, **k: None),
            mock.patch.object(deps, "audit_log", lambda *a, **k: None),
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
        for p in self.patches:
            p.stop()
        APP_CONFIG["capabilities"] = self.saved_caps
        self.tmp.cleanup()

    def _skill(self):
        items = self.client.get("/customize/skills").json()["items"]
        return next(i for i in items if i["id"] == "demo")

    def test_list_install_uninstall_skill(self):
        d = self.client.get("/customize/skills").json()
        self.assertEqual(d["categories"], {"security": 1})
        self.assertFalse(self._skill()["installed"])
        r = self.client.post("/customize/skills/demo/install")
        self.assertEqual(r.status_code, 200)
        self.assertTrue((self.installed / "demo" / "SKILL.md").is_file())
        self.assertTrue(self._skill()["can_uninstall"])
        self.assertEqual(self.client.delete("/customize/skills/demo").status_code, 200)
        self.assertFalse((self.installed / "demo").exists())

    def test_modified_skill_is_not_removed(self):
        self.client.post("/customize/skills/demo/install")
        (self.installed / "demo" / "SKILL.md").write_text("edited", encoding="utf-8")
        self.assertTrue(self._skill()["modified"])
        self.assertEqual(self.client.delete("/customize/skills/demo").status_code, 400)
        self.assertTrue((self.installed / "demo").exists())

    def test_invalid_names_rejected(self):
        self.assertEqual(self.client.post("/customize/skills/Bad.Name/install").status_code, 400)
        self.assertEqual(self.client.get("/customize/skills/..%5Cx/preview").status_code, 400)
        self.assertEqual(self.client.post("/customize/skills/nope/install").status_code, 400)
        self.assertEqual(self.client.get("/customize/widgets").status_code, 404)

    def test_install_needs_permission_browse_does_not(self):
        self.perms.clear()
        self.assertEqual(self.client.get("/customize/skills").status_code, 200)
        self.assertEqual(self.client.post("/customize/skills/demo/install").status_code, 403)
        self.assertFalse((self.installed / "demo").exists())

    def test_connector_requires_declared_secrets(self):
        r = self.client.post("/customize/connectors/github/install", json={"secrets": {}})
        self.assertEqual(r.status_code, 400)
        self.assertIn("GITHUB_PERSONAL_ACCESS_TOKEN", r.json()["error"])
        r = self.client.post("/customize/connectors/not-a-preset/install", json={})
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
