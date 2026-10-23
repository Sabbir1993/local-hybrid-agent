"""The Code tab's file tree: an agent's own work folder in Agent mode, the project in Code mode, never one for the other.

Run: python -m unittest tests.test_ws_tree_agent -v
"""
import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routes.agent import ws_browse

USER = SimpleNamespace(id=7)
FOLDER = Path("C:/Agents/System Agent")


def run(coro):
    r = asyncio.run(coro)
    return (r.status_code, json.loads(r.body)) if hasattr(r, "body") else (200, r)


class AgentTreeTests(unittest.TestCase):
    def setUp(self):
        self.changes = {}
        self.patches = [
            mock.patch.object(ws_browse, "_ws_changes", {7: self.changes}),
            mock.patch.object(ws_browse, "get_active_project", return_value="3d website"),
            mock.patch.object(ws_browse, "_ws_tree_scan", mock.AsyncMock(return_value=[{"name": "report.md", "path": "report.md", "dir": False}])),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def _agent(self, work_dir):
        return mock.patch("core.auth_db.db_get_custom_agent", return_value={"user_id": 7, "work_dir": work_dir})

    def test_an_agent_without_a_folder_is_an_error_not_the_project(self):
        with self._agent(""), mock.patch.object(ws_browse, "active_workspace", return_value=Path("D:/Projects/3d website")) as ws:
            status, body = run(ws_browse.agent_ws_tree("", USER, agent=5))
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "This agent has no work folder")
        ws.assert_not_called()                          # the project folder was never even looked up

    def test_an_agent_with_a_folder_shows_that_folder(self):
        with self._agent(str(FOLDER)), mock.patch.object(ws_browse, "active_workspace", return_value=FOLDER):
            status, body = run(ws_browse.agent_ws_tree("", USER, agent=5))
        self.assertEqual(status, 200)
        self.assertEqual((body["root"], body["project"], body["agent"]), ("System Agent", None, 5))
        self.assertEqual([n["name"] for n in body["nodes"]], ["report.md"])

    def test_session_changes_list_only_files_of_the_shown_folder(self):
        self.changes[str(FOLDER / "notes" / "a.md")] = {"before": None, "after": "x"}
        self.changes["D:\\Projects\\3d website\\src\\cart.js"] = {"before": "a", "after": "b"}
        with self._agent(str(FOLDER)), mock.patch.object(ws_browse, "active_workspace", return_value=FOLDER):
            _, body = run(ws_browse.agent_ws_tree("", USER, agent=5))
        self.assertEqual(body["changes"], [{"path": "notes/a.md", "status": "created"}])

    def test_another_users_agent_is_not_readable(self):
        with mock.patch("core.auth_db.db_get_custom_agent", return_value={"user_id": 99, "work_dir": "C:/Secret"}):
            status, body = run(ws_browse.agent_ws_tree("", USER, agent=5))
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "This agent has no work folder")

    def test_without_an_agent_the_project_is_used_as_before(self):
        proj = Path("D:/Projects/3d website")
        with mock.patch.object(ws_browse, "active_workspace", return_value=proj):
            status, body = run(ws_browse.agent_ws_tree("", USER))
        self.assertEqual((status, body["root"], body["project"], body["agent"]), (200, "3d website", "3d website", None))

    def test_no_agent_and_no_project(self):
        with mock.patch.object(ws_browse, "get_active_project", return_value=None):
            status, body = run(ws_browse.agent_ws_tree("", USER))
        self.assertEqual(body["error"], "No project selected")

    def test_a_disconnected_companion_says_so(self):
        denied = ws_browse.WorkspaceAccessDenied("the SSL Local Agent app is not connected - open it on your machine and try again")
        with self._agent(str(FOLDER)), mock.patch.object(ws_browse, "active_workspace", side_effect=denied):
            status, body = run(ws_browse.agent_ws_tree("", USER, agent=5))
        self.assertEqual(status, 409)
        self.assertIn("not connected", body["error"])


if __name__ == "__main__":
    unittest.main()
