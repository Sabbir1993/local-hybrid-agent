"""tests/test_git_routes.py - E2: git panel routes, companion-routed (2026-10-07).

git runs on the USER's machine via the companion (companion/gitops.js), not on
the server. The routes await git_tools fns that issue one
`companion_bridge.call(uid, "git.run", ...)` RPC each; this suite mocks the
bridge with canned git output and exercises the real parsers (status header
parsing, branch resolution, diff cap), plus the git.push permission gate.

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


class CompanionRoutedGitTests(_GitApp):
    """With the companion bridge mocked, the route surface works end to end."""

    def _bridge(self, output_map):
        """Mock companion_bridge.call: args key -> canned {exit_code, stdout}."""
        def fake_call(uid, op, params, timeout=None):
            args = tuple(params.get("args") or [])
            rec = output_map.get(args, {"exit_code": 0, "stdout": "", "stderr": ""})
            return rec
        patcher = mock.patch("core.git_tools.companion_bridge.call", side_effect=fake_call)
        patcher.start()
        self.addCleanup(patcher.stop)
        # current user id + workspace come from request context
        patcher2 = mock.patch("core.git_tools.get_current_user_id", return_value=7)
        patcher2.start()
        self.addCleanup(patcher2.stop)
        patcher3 = mock.patch(
            "core.git_tools.require_device_workspace", return_value=(7, Path("C:/ws")))
        patcher3.start()
        self.addCleanup(patcher3.stop)

    def test_status_parses_branch_and_files(self):
        self._bridge({
            ("rev-parse", "--is-inside-work-tree"): {"exit_code": 0, "stdout": "true", "stderr": ""},
            ("status", "--porcelain=v1", "-b"): {"exit_code": 0, "stdout": "## main\n M a.py\n?? b.py", "stderr": ""},
            ("rev-parse", "--abbrev-ref", "HEAD"): {"exit_code": 0, "stdout": "main", "stderr": ""},
        })
        r = self.client.get("/git/status")
        self.assertEqual(r.status_code, 200, r.text[:200])
        body = r.json()
        self.assertEqual(body["branch"], "main")
        self.assertFalse(body["detached"])
        self.assertEqual(len(body["files"]), 2)

    def test_status_reports_non_repo(self):
        self._bridge({
            ("rev-parse", "--is-inside-work-tree"): {"exit_code": 0, "stdout": "false", "stderr": ""},
        })
        r = self.client.get("/git/status")
        self.assertEqual(r.status_code, 400)
        self.assertIn("not a git repository", r.json()["error"])

    def test_bridge_refusal_surfaces_400(self):
        # a destructive arg is refused by the server (returns exit_code 1 with
        # a reason) before any RPC reaches the companion
        def fake_call(uid, op, params, timeout=None):
            return {"exit_code": 1, "stdout": "",
                    "stderr": "not a git repository"}
        mock.patch("core.git_tools.companion_bridge.call", side_effect=fake_call).start()
        patch_repo = mock.patch("core.git_tools.require_device_workspace",
                                return_value=(7, Path("C:/ws")))
        patch_repo.start()
        self.addCleanup(mock.patch.stopall)
        r = self.client.get("/git/diff")
        self.assertEqual(r.status_code, 400, r.text[:200])

    def test_destructive_arg_is_refused_server_side(self):
        import asyncio
        from core import git_tools
        called = {"n": 0}

        def fake_call(uid, op, params, timeout=None):
            called["n"] += 1
            return {"exit_code": 0, "stdout": "", "stderr": ""}
        with mock.patch("core.git_tools.companion_bridge.call", side_effect=fake_call), \
             mock.patch("core.git_tools.get_current_user_id", return_value=7):
            code, _out, err = asyncio.run(
                git_tools._run(["--hard"], Path("C:/ws")))
        self.assertEqual(code, 1)
        self.assertIn("refused", err)
        self.assertEqual(called["n"], 0)   # no RPC was sent

    def test_checkpoints_and_rewind(self):
        import asyncio, tempfile
        from core.agent_loop import checkpoint
        tmp = tempfile.TemporaryDirectory()
        checkpoint.USAGE_DB_FILE = Path(tmp.name) / "u.db"
        def _cleanup():
            try:
                checkpoint._checkpoint_db().close()
            except Exception:
                pass
            checkpoint.USAGE_DB_FILE = checkpoint.USAGE_DB_FILE  # keep path as-is
            tmp.cleanup()
        self.addCleanup(_cleanup)

        async def git(args):
            return 0, "cafe1234\n", ""
        # record a checkpoint for user 7
        asyncio.run(checkpoint.create_checkpoint("run-aa", 7, "sess-1", 0, git))

        r = self.client.get("/git/checkpoints?session_id=sess-1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["checkpoints"][0]["run_id"], "run-aa")

        # rewind drives git through git_tools (companion-routed); mock _run.
        def fake_run(args, cwd):
            self.assertIn("--hard", args)
            return 0, "", ""
        with mock.patch("core.git_tools._run", side_effect=fake_run), \
             mock.patch("core.git_tools.require_device_workspace", return_value=(7, Path("C:/ws"))):
            r = self.client.post("/git/rewind", json={"run_id": "run-aa", "step": 0})
        self.assertEqual(r.status_code, 200, r.text[:200])
        self.assertEqual(r.json()["rewound_to"], "cafe1234")

    def test_commit_success(self):
        self._bridge({
            ("rev-parse", "--is-inside-work-tree"): {"exit_code": 0, "stdout": "true", "stderr": ""},
            ("commit", "-m", "fix"): {"exit_code": 0, "stdout": "[main abc1234] fix", "stderr": ""},
        })
        r = self.client.post("/git/commit", json={"message": "fix"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

    def test_branches(self):
        self._bridge({
            ("rev-parse", "--is-inside-work-tree"): {"exit_code": 0, "stdout": "true", "stderr": ""},
            ("for-each-ref", "--format=%(refname:short)", "refs/heads/"): {"exit_code": 0, "stdout": "main\nfeature\n", "stderr": ""},
            ("symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"): {"exit_code": 0, "stdout": "origin/main", "stderr": ""},
        })
        r = self.client.get("/git/branches")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["branches"], ["main", "feature"])
        self.assertEqual(r.json()["default"], "main")


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
