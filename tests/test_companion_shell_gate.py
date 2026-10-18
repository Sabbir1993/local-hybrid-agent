"""tests/test_companion_shell_gate.py - the dock-terminal endpoint posture.

POST /control/companion/shell used to be the one RCE-shaped hole in an
otherwise layered shell story: session-only (any chat.use-free user too),
no always-refused check, no audit trail, and a server-side local fallback
that executed arbitrary commands ON THE SERVER when no companion was
connected - the inverse of the "agent commands never run on the server"
invariant the rest of the codebase enforces. These tests pin the rewrite:

1. chat.use permission is demanded (require_permission, not bare session).
2. The always-refused list holds even here.
3. No companion -> 400 is_remote, never a local subprocess spawn.
4. Companion path is audited with the command NAME only, not the full text.
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


class CompanionShellGate(unittest.TestCase):
    def _client(self, principal, registry=None):
        from routes.projects import companion_endpoints as ce
        app = FastAPI()
        app.include_router(ce.router)
        # require_permission("chat.use") returns a fresh closure per call, so the
        # override must land on get_current_user, which that closure Depends on
        app.dependency_overrides[deps.get_current_user] = lambda: principal
        client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(client.close)
        return client

    def test_has_the_permission_gate(self):
        src = Path(Path(__file__).resolve().parents[1] / "routes"
                   / "projects" / "companion_endpoints.py").read_text(encoding="utf-8")
        self.assertIn("require_permission(\"chat.use\")", src)

    def test_no_server_fallback_exists(self):
        src = Path(Path(__file__).resolve().parents[1] / "routes"
                   / "projects" / "companion_endpoints.py").read_text(encoding="utf-8")
        self.assertNotIn("create_subprocess_exec", src,
                         "the server-side local fallback must stay deleted")
        self.assertNotIn("powershell.exe\", \"-NoProfile", src)

    def test_always_refused_holds_for_manual_terminal(self):
        principal = _principal(3, {"chat.use"})
        with mock.patch("routes.projects.companion_endpoints.companion_bridge") as cb, \
             mock.patch("routes.projects.companion_endpoints.audit_log") as au:
            cb.is_connected.return_value = True
            client = self._client(principal)
            r = client.post("/control/companion/shell", json={
                "command": "format C: /Q /Y", "timeout": 5})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("ok", r.json())
        self.assertFalse(r.json()["ok"])
        au.assert_called_once()
        self.assertEqual(au.call_args.kwargs.get("result"), "deny")
        self.assertEqual(cb.call.call_count, 0)

    def test_no_companion_is_remote_400(self):
        principal = _principal(3, {"chat.use"})
        with mock.patch("routes.projects.companion_endpoints.companion_bridge") as cb:
            cb.is_connected.return_value = False
            client = self._client(principal)
            r = client.post("/control/companion/shell", json={"command": "git status"})
        self.assertEqual(r.status_code, 400)
        self.assertTrue(r.json()["is_remote"])
        cb.call.assert_not_called()

    def test_companion_path_audits_name_only(self):
        principal = _principal(3, {"chat.use"})
        with mock.patch("routes.projects.companion_endpoints.companion_bridge") as cb, \
             mock.patch("routes.projects.companion_endpoints.audit_log") as au:
            cb.is_connected.return_value = True
            cb.call = mock.AsyncMock(return_value={"exit_code": 0, "stdout": "", "stderr": ""})
            client = self._client(principal)
            r = client.post("/control/companion/shell", json={
                "command": "git push --credentials MY_SECRET_TOKEN", "timeout": 5})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["source"], "companion")
        detail = au.call_args.kwargs.get("detail") or {}
        self.assertEqual(detail.get("command_name"), "git")
        self.assertNotIn("MY_SECRET_TOKEN", str(au.call_args))
        self.assertEqual(cb.call.call_count, 1)

    def test_empty_command_400(self):
        principal = _principal(3, {"chat.use"})
        client = self._client(principal)
        r = client.post("/control/companion/shell", json={"command": "  "})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
