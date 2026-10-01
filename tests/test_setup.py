"""tests/test_setup.py - behavioral tests for the per-request setup extraction.

Run: python -m unittest tests.test_setup -v

routes/agent/setup.py holds the guards, lane resolution, and prompt assembly that used
to be inline in agent_run()'s preamble. Because setup_run() is directly drivable (unlike
the 2000-line generator it came from), the refusal branches get behavioral tests here -
not source-text assertions. Each test drives setup_run with mocks and asserts the exact
SetupError payload and status the route returns as JSON.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routes.agent.models import AgentRequest
from routes.agent.setup import RunContext, SetupError, setup_run


def run(coro):
    return asyncio.run(coro)


def make_req(**kw):
    base = {"messages": [{"role": "user", "content": "hi"}]}
    base.update(kw)
    return AgentRequest(**base)


def make_request(ua="A770NativeApp/1.0"):
    req = mock.Mock()
    req.headers = {"user-agent": ua}
    req.client = mock.Mock(host="127.0.0.1")
    return req


def make_user(uid=7):
    return SimpleNamespace(id=uid)


class SetupGuards(unittest.TestCase):
    def test_browser_ua_is_refused_before_any_io(self):
        # first guard in the function: no DB, companion, or model touched
        with mock.patch("routes.agent.setup.db_session_owner",
                        side_effect=AssertionError("must not reach DB")), \
             mock.patch("routes.agent.setup.companion_bridge") as cb:
            cb.is_connected.side_effect = AssertionError("must not reach companion")
            with self.assertRaises(SetupError) as cm:
                run(setup_run(make_req(), make_request(
                    ua="Mozilla/5.0 (Windows NT 10.0; Win64; x64)"), make_user()))
        self.assertEqual(cm.exception.status_code, 403)
        self.assertEqual(cm.exception.payload["error"], "agent_native_only")

    def test_session_owned_by_someone_else_is_hidden(self):
        with mock.patch("routes.agent.setup.db_session_owner", return_value=9):
            with self.assertRaises(SetupError) as cm:
                run(setup_run(make_req(session_id=12), make_request(), make_user(7)))
        self.assertEqual(cm.exception.status_code, 404)
        self.assertEqual(cm.exception.payload["error"], "session not found")

    def test_own_session_passes_isolation(self):
        # same owner: no error from this guard (setup continues past it; it will fail
        # later at the companion check, which proves this guard passed)
        with mock.patch("routes.agent.setup.db_session_owner", return_value=7), \
             mock.patch("routes.agent.setup.companion_bridge") as cb:
            cb.is_connected.return_value = False
            with self.assertRaises(SetupError) as cm:
                run(setup_run(make_req(session_id=12), make_request(), make_user(7)))
        self.assertEqual(cm.exception.payload["error"], "agent_requires_companion")

    def test_personal_agent_without_folder_is_refused(self):
        agent = {"user_id": 7, "work_dir": ""}
        with mock.patch("routes.agent.setup.db_session_owner", return_value=None), \
             mock.patch("core.auth_db.db_get_custom_agent", return_value=agent):
            with self.assertRaises(SetupError) as cm:
                run(setup_run(make_req(personal=True, custom_agent_id=3),
                              make_request(), make_user(7)))
        self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(cm.exception.payload["error"], "personal_needs_folder")

    def test_personal_agent_with_another_owners_folder_is_refused(self):
        agent = {"user_id": 9, "work_dir": "/somewhere/else"}
        with mock.patch("routes.agent.setup.db_session_owner", return_value=None), \
             mock.patch("core.auth_db.db_get_custom_agent", return_value=agent):
            with self.assertRaises(SetupError) as cm:
                run(setup_run(make_req(personal=True, custom_agent_id=3),
                              make_request(), make_user(7)))
        self.assertEqual(cm.exception.payload["error"], "personal_needs_folder")

    def test_missing_companion_is_refused(self):
        with mock.patch("routes.agent.setup.db_session_owner", return_value=None), \
             mock.patch("routes.agent.setup.companion_bridge") as cb:
            cb.is_connected.return_value = False
            with self.assertRaises(SetupError) as cm:
                run(setup_run(make_req(), make_request(), make_user()))
        self.assertEqual(cm.exception.status_code, 403)
        self.assertEqual(cm.exception.payload["error"], "agent_requires_companion")

    def test_missing_custom_agent_is_not_found(self):
        with mock.patch("routes.agent.setup.db_session_owner", return_value=None), \
             mock.patch("core.auth_db.db_get_custom_agent", return_value=None):
            with self.assertRaises(SetupError) as cm:
                run(setup_run(make_req(custom_agent_id=999), make_request(), make_user()))
        self.assertEqual(cm.exception.status_code, 404)
        self.assertEqual(cm.exception.payload["error"], "custom_agent_not_found")


class SetupErrorShape(unittest.TestCase):
    def test_payload_and_status_survive(self):
        e = SetupError({"error": "x"}, 418)
        self.assertEqual(e.payload, {"error": "x"})
        self.assertEqual(e.status_code, 418)
        self.assertIn("x", str(e))

    def test_context_defaults(self):
        ctx = RunContext()
        self.assertEqual(ctx.mode, "all-local")
        self.assertFalse(ctx.personal)
        self.assertEqual(ctx.hidden_tools, frozenset())


if __name__ == "__main__":
    unittest.main()
