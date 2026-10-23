"""Administrator ("System") terminal: the server gate, session expiry, audit and the not-auto-granted permission.

Run: python -m unittest tests.test_admin_terminal -v
"""
import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import admin_terminal
from core.auth_db import common
from routes.projects import companion_endpoints as ce
from routes.projects.models import ShellExecReq

USER = SimpleNamespace(id=7)
REQ = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))


def call(coro):
    r = asyncio.run(coro)
    return (r.status_code, json.loads(r.body)) if hasattr(r, "body") else (200, r)


class SessionTests(unittest.TestCase):
    def setUp(self):
        admin_terminal.clear()
        self.addCleanup(admin_terminal.clear)

    def test_off_until_started(self):
        self.assertFalse(admin_terminal.active(7))
        admin_terminal.start(7)
        self.assertTrue(admin_terminal.active(7))
        self.assertFalse(admin_terminal.active(8))       # per person

    def test_idle_ends_the_session(self):
        admin_terminal.start(7)
        with mock.patch("core.admin_terminal.time.time", return_value=admin_terminal._sessions[7]["last"] + admin_terminal.idle_seconds() + 1):
            self.assertFalse(admin_terminal.active(7))
        self.assertNotIn(7, admin_terminal._sessions)    # removed, not just reported

    def test_use_extends_it(self):
        admin_terminal.start(7)
        t0 = admin_terminal._sessions[7]["last"]
        with mock.patch("core.admin_terminal.time.time", return_value=t0 + admin_terminal.idle_seconds() - 5):
            admin_terminal.touch(7)
        with mock.patch("core.admin_terminal.time.time", return_value=t0 + admin_terminal.idle_seconds() + 10):
            self.assertTrue(admin_terminal.active(7))

    def test_end(self):
        admin_terminal.start(7)
        self.assertTrue(admin_terminal.end(7))
        self.assertFalse(admin_terminal.end(7))
        self.assertFalse(admin_terminal.active(7))


class ShellGateTests(unittest.TestCase):
    def setUp(self):
        admin_terminal.clear()
        self.addCleanup(admin_terminal.clear)
        self.audit = mock.patch.object(ce, "audit_log")
        self.audit_mock = self.audit.start()
        self.addCleanup(self.audit.stop)

    def _shell(self, **kw):
        req = ShellExecReq(command=kw.pop("command", "Get-Service"), cwd="C:/", shell=kw.pop("shell", "powershell"), **kw)
        return call(ce.companion_shell(REQ, req, USER))

    def test_without_the_permission_is_403(self):
        with mock.patch("core.auth.user_has_permission", return_value=False):
            status, body = self._shell(elevated=True)
        self.assertEqual(status, 403)
        self.assertIn("terminal.admin", body["error"])

    def test_without_a_live_session_is_403_and_never_reaches_the_pc(self):
        with mock.patch("core.auth.user_has_permission", return_value=True), \
                mock.patch.object(ce.companion_bridge, "call", mock.AsyncMock()) as bridge, \
                mock.patch.object(ce.companion_bridge, "is_connected", return_value=True):
            status, body = self._shell(elevated=True)
        self.assertEqual(status, 403)
        self.assertTrue(body["admin_required"])
        bridge.assert_not_called()

    def test_with_a_session_it_runs_elevated_and_is_audited(self):
        admin_terminal.start(7)
        bridge = mock.AsyncMock(return_value={"exit_code": 0, "stdout": "ok", "stderr": ""})
        with mock.patch("core.auth.user_has_permission", return_value=True), \
                mock.patch.object(ce.companion_bridge, "call", bridge), \
                mock.patch.object(ce.companion_bridge, "is_connected", return_value=True):
            status, body = self._shell(elevated=True, command="Get-Service secret-arg")
        self.assertEqual(status, 200)
        self.assertTrue(body["elevated"])
        self.assertEqual(bridge.call_args.args[1], "shell.admin_run")
        kw = self.audit_mock.call_args.kwargs
        self.assertEqual(kw["action"], "companion.shell.admin")
        self.assertEqual(kw["detail"]["command_name"], "get-service")
        self.assertNotIn("secret-arg", json.dumps(kw["detail"]))     # the first word only

    def test_a_regular_command_is_never_elevated(self):
        admin_terminal.start(7)                                       # even with a live admin session
        bridge = mock.AsyncMock(return_value={"exit_code": 0, "stdout": "", "stderr": ""})
        with mock.patch.object(ce.companion_bridge, "call", bridge), mock.patch.object(ce.companion_bridge, "is_connected", return_value=True):
            status, body = self._shell()
        self.assertEqual((status, body["elevated"]), (200, False))
        self.assertEqual(bridge.call_args.args[1], "shell.run")

    def test_bash_is_refused_in_admin_mode(self):
        admin_terminal.start(7)
        with mock.patch("core.auth.user_has_permission", return_value=True):
            status, _ = self._shell(elevated=True, shell="bash")
        self.assertEqual(status, 400)


class PermissionTests(unittest.TestCase):
    def test_it_is_defined_and_not_auto_granted(self):
        self.assertIn("terminal.admin", common.PERMISSIONS)
        self.assertIn("terminal.admin", common._NOT_AUTO_GRANTED)


if __name__ == "__main__":
    unittest.main()
