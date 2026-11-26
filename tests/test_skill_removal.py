"""tests/test_skill_removal.py - removing skills (and plugins) from Customize.

A skill that is exactly what the catalog installed can always be removed, even after the catalog moved on (it used to read as
"modified" and was stuck). A hand-written or edited one is removed only after the person confirms (force), because its files
are deleted for good.

Run: python -m unittest tests.test_skill_removal -v
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from core import deps
from core import skills as sk
from core.auth import Principal

BODY = "---\nname: demo\ndescription: a demo skill\n---\nDo the demo.\n"


def _principal(perms):
    return Principal(id=7, username="u7", display_name="u7", is_super_admin=False,
                     must_change_password=False, role_names=[], permission_keys=set(perms))


class SkillDirs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.catalog, self.skills = root / "catalog", root / "skills"
        (self.catalog / "demo").mkdir(parents=True)
        (self.catalog / "demo" / "SKILL.md").write_text(BODY, encoding="utf-8")
        for patcher in (mock.patch.object(sk, "CATALOG_DIR", self.catalog), mock.patch.object(sk, "SKILLS_DIR", self.skills)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def installed(self):
        return (self.skills / "demo").exists()


class Uninstall(SkillDirs):
    def test_an_untouched_install_is_removed_without_asking(self):
        sk.install_from_catalog("demo")
        sk.uninstall("demo")
        self.assertFalse(self.installed())

    def test_an_install_the_catalog_has_moved_on_from_can_still_be_removed(self):
        sk.install_from_catalog("demo")
        (self.catalog / "demo" / "SKILL.md").write_text(BODY + "A newer line.\n", encoding="utf-8")
        self.assertTrue(sk.is_modified("demo"), "it differs from the catalog's current copy ...")
        self.assertEqual(sk.trust_of("demo"), "reviewed", "... but nobody edited it")
        sk.uninstall("demo")                                   # this used to raise "hand-written or locally modified"
        self.assertFalse(self.installed())

    def test_a_locally_edited_skill_needs_confirmation_then_goes(self):
        sk.install_from_catalog("demo")
        (self.skills / "demo" / "SKILL.md").write_text(BODY + "My own edit.\n", encoding="utf-8")
        with self.assertRaises(sk.NeedsForce) as cm:
            sk.uninstall("demo")
        self.assertIn("for good", str(cm.exception))
        self.assertTrue(self.installed(), "a refused removal deletes nothing")
        sk.uninstall("demo", force=True)
        self.assertFalse(self.installed())

    def test_a_hand_written_skill_needs_confirmation_then_goes(self):
        (self.skills / "mine").mkdir(parents=True)
        (self.skills / "mine" / "SKILL.md").write_text("---\nname: mine\n---\nmine\n", encoding="utf-8")
        with self.assertRaises(sk.NeedsForce):
            sk.uninstall("mine")
        sk.uninstall("mine", force=True)
        self.assertFalse((self.skills / "mine").exists())

    def test_not_installed_and_bad_names_are_plain_errors(self):
        with self.assertRaises(ValueError) as cm:
            sk.uninstall("demo", force=True)
        self.assertNotIsInstance(cm.exception, sk.NeedsForce)
        with self.assertRaises(ValueError):
            sk.uninstall("../etc", force=True)


class Endpoint(SkillDirs):
    @classmethod
    def setUpClass(cls):
        from server_manager import app
        cls.app = app

    def delete(self, url, perms):
        self.app.dependency_overrides[deps.get_current_user] = lambda: _principal(perms)
        self.addCleanup(self.app.dependency_overrides.pop, deps.get_current_user, None)
        c = TestClient(self.app, raise_server_exceptions=False)
        self.addCleanup(c.close)
        with mock.patch("routes.customize.install_endpoints.audit_log") as audit, mock.patch.object(deps, "audit_log"):
            r = c.delete(url)
        return r, audit

    def test_edited_skill_answers_409_then_force_removes_it_and_is_audited(self):
        sk.install_from_catalog("demo")
        (self.skills / "demo" / "SKILL.md").write_text(BODY + "edit\n", encoding="utf-8")
        r, _ = self.delete("/customize/skills/demo", {"capabilities.install"})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertTrue(r.json()["needs_force"])
        self.assertTrue(self.installed())
        r, audit = self.delete("/customize/skills/demo?force=true", {"capabilities.install"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(self.installed())
        self.assertEqual(audit.call_args.kwargs.get("detail"), {"force": True})

    def test_force_does_not_skip_the_install_permission(self):
        sk.install_from_catalog("demo")
        (self.skills / "demo" / "SKILL.md").write_text(BODY + "edit\n", encoding="utf-8")
        r, _ = self.delete("/customize/skills/demo?force=true", {"skills.view"})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertTrue(self.installed())

    def test_listing_calls_a_moved_on_install_an_update_not_a_modification(self):
        from routes.customize.listing import _skills_items
        sk.install_from_catalog("demo")
        (self.catalog / "demo" / "SKILL.md").write_text(BODY + "newer\n", encoding="utf-8")
        item = next(i for i in _skills_items(7) if i["id"] == "demo")
        self.assertFalse(item["modified"])
        self.assertTrue(item["update_available"])
        self.assertTrue(item["can_uninstall"])
        (self.skills / "demo" / "SKILL.md").write_text(BODY + "mine\n", encoding="utf-8")
        item = next(i for i in _skills_items(7) if i["id"] == "demo")
        self.assertTrue(item["modified"])
        self.assertFalse(item["update_available"])


if __name__ == "__main__":
    unittest.main()
