"""tests/test_git_routes.py - E2: git panel routes (previously 29% covered, no test file).

Local git runs on the server while project folders live on user machines, so
every git_tools helper raises WorkspaceAccessDenied. The routes must surface
that reason as a 400 (not an unhandled 500), keep the git.push gates, and map
the error paths of the suggest/PR flows.

Run: python -m unittest tests.test_git_routes -v
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
from routes import git as git_routes


def _principal(perms):
    return Principal(id=7, username="u7", display_name="u7", is_super_admin=False,
                     must_change_password=False, role_names=[], permission_keys=set(perms))


class _GitApp(unittest.TestCase):
    perms = {"chat.use"}

    def setUp(self):
        app = FastAPI()
        app.include_router(git_routes.router)
        app.dependency_overrides[deps.get_current_user] = lambda: _principal(self.perms)
        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self):
        self.client.close()


class DisabledPanelTests(_GitApp):
    """The panel is companion-routed: every local op reports why, as a 400."""

    def test_reads_report_unavailable(self):
        for method, path, kwargs in (
                ("GET", "/git/status", {}),
                ("GET", "/git/branches", {}),
                ("GET", "/git/diff", {})):
            with self.subTest(path=path):
                r = self.client.request(method, path, **kwargs)
                self.assertEqual(r.status_code, 400, r.text[:200])
                self.assertIn("Git panel is unavailable", r.json()["error"])

    def test_writes_report_unavailable(self):
        for path, body in (("/git/stage", {"paths": ["a.py"]}),
                           ("/git/unstage", {"paths": ["a.py"]}),
                           ("/git/commit", {"message": "x"})):
            with self.subTest(path=path):
                r = self.client.post(path, json=body)
                self.assertEqual(r.status_code, 400, r.text[:200])

    def test_suggest_flows_report_unavailable(self):
        r = self.client.post("/git/suggest_commit_message")
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/git/suggest_pr", json={"base": "main"})
        self.assertEqual(r.status_code, 400)


class PermissionTests(_GitApp):
    def test_push_and_pr_need_git_push(self):
        self.assertEqual(self.client.post("/git/push", json={}).status_code, 403)
        self.assertEqual(self.client.post("/git/pr", json={"title": "t"}).status_code, 403)

    def test_pr_without_github_reports_not_connected(self):
        self.perms = {"chat.use", "git.push"}
        with mock.patch.object(git_routes.mcp_core, "is_ready", lambda *a: False):
            r = self.client.post("/git/pr", json={"title": "t"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("not connected", r.json()["error"].lower())


if __name__ == "__main__":
    unittest.main()
