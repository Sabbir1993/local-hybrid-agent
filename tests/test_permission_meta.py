"""Every permission key has plain-language metadata for the Users & Roles screen."""
import unittest

from core.auth_db.common import PERMISSIONS, PERMISSION_META, PERMISSION_MODULES


class PermissionMetaTests(unittest.TestCase):
    def test_every_permission_is_described(self):
        self.assertEqual(set(PERMISSIONS), set(PERMISSION_META))

    def test_modules_and_kinds_are_known(self):
        modules = {m for m, _ in PERMISSION_MODULES}
        for key, (module, kind, title, help_) in PERMISSION_META.items():
            self.assertIn(module, modules, key)
            self.assertIn(kind, ("read", "write", "action"), key)
            self.assertTrue(title and help_, key)

    def test_read_permissions_are_view_only_wording(self):
        for key, (_, kind, _, help_) in PERMISSION_META.items():
            if kind == "read":
                self.assertIn("view only", help_.lower(), key)


if __name__ == "__main__":
    unittest.main()
