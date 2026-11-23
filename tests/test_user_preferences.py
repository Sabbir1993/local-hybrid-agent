"""Per-user Appearance preferences are scoped to the account and validated."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import auth_db, user_preferences


class AppearancePreferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = auth_db._auth_db
        with mock.patch.object(auth_db, "AUTH_DB_FILE", Path(self.tmp.name) / "auth.db"):
            auth_db._auth_db = auth_db._init_auth_db()
        self.addCleanup(self._restore)

    def _restore(self):
        try:
            auth_db._auth_db.close()
        except Exception:
            pass
        auth_db._auth_db = self.saved
        self.tmp.cleanup()

    def test_defaults_and_scoping(self):
        self.assertEqual(user_preferences.get(1)["font_size"], 14)
        user_preferences.save(1, {"accent": "#123456", "font_size": 18, "mode": "dark"})
        self.assertEqual(user_preferences.get(1)["accent"], "#123456")
        self.assertEqual(user_preferences.get(1)["font_size"], 18)
        self.assertEqual(user_preferences.get(2)["accent"], "#2F71E3")  # other user untouched

    def test_invalid_values_are_ignored(self):
        user_preferences.save(3, {"mode": "hack", "accent": "red;drop", "font_size": 999})
        prefs = user_preferences.get(3)
        self.assertEqual(prefs["mode"], "system")
        self.assertEqual(prefs["accent"], "#2F71E3")
        self.assertEqual(prefs["font_size"], 20)


if __name__ == "__main__":
    unittest.main()
