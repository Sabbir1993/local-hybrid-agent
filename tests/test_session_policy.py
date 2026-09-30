"""Idle sign-out is an admin setting (security.session_idle_enabled / session_idle_minutes)."""
import unittest
from unittest import mock

from core import auth


class SessionPolicyTests(unittest.TestCase):
    def _policy(self, sec):
        with mock.patch.dict("core.small_model.APP_CONFIG", {"security": sec}):
            return auth.session_policy(), auth.session_idle_s()

    def test_defaults_are_on_at_15_minutes(self):
        p, s = self._policy({})
        self.assertEqual(p, {"enabled": True, "minutes": 15})
        self.assertEqual(s, 15 * 60)

    def test_admin_can_raise_the_idle_time(self):
        p, s = self._policy({"session_idle_minutes": 120})
        self.assertEqual((p["minutes"], s), (120, 120 * 60))

    def test_idle_time_is_clamped(self):
        self.assertEqual(self._policy({"session_idle_minutes": 0.2})[0]["minutes"], auth.IDLE_MIN_MINUTES)
        self.assertEqual(self._policy({"session_idle_minutes": 99999})[0]["minutes"], auth.IDLE_MAX_MINUTES)
        self.assertEqual(self._policy({"session_idle_minutes": "junk"})[0]["minutes"], 15)

    def test_disabled_keeps_the_session_to_the_absolute_cap(self):
        p, s = self._policy({"session_idle_enabled": False, "session_idle_minutes": 30})
        self.assertFalse(p["enabled"])
        self.assertEqual(s, auth.SESSION_ABSOLUTE_MAX_S)


if __name__ == "__main__":
    unittest.main()
