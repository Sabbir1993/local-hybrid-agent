"""tests/test_workspace_guard.py - agent tools never fall back to the server's disk.

Policy: agent workspace access resolves ONLY to the calling user's project on
the calling device, executed through the companion app. Every missing piece
must raise WorkspaceAccessDenied instead of yielding WORKSPACE_ROOT or a
server sandbox path.

Run: python -m unittest tests.test_workspace_guard -v
"""

import asyncio
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import agent_tools, companion_bridge, memory
from core.agent_tools import WorkspaceAccessDenied, require_device_workspace
from core.request_context import set_current_user, set_current_device

USER_DIR = r"C:\Users\[PLACEHOLDER]\projects\ui-inspector"


class GuardTests(unittest.TestCase):
    def setUp(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE projects (name TEXT, user_id INTEGER, device_id TEXT, workspace_dir TEXT)")
        db.executemany("INSERT INTO projects VALUES (?, ?, ?, ?)", [
            ("ui-inspector", 7, "dev_laptop", USER_DIR),
            ("sandbox", 7, "dev_laptop", None),              # server-sandbox style project
            ("other", 8, "dev_laptop", r"C:\other\user"),    # another user's project
        ])
        self.connected = {7}
        self._patches = [
            mock.patch.object(agent_tools, "_projects_db", db),
            mock.patch.object(companion_bridge, "is_connected", lambda uid: uid in self.connected),
            mock.patch.dict(agent_tools._active_project, {}, clear=True),
        ]
        for p in self._patches:
            p.start()
        set_current_user(7)
        set_current_device("dev_laptop")

    def tearDown(self):
        for p in self._patches:
            p.stop()
        set_current_user(None)
        set_current_device(None)

    def activate(self, name, uid=7, did="dev_laptop"):
        agent_tools._active_project[f"{uid}:{did}"] = name

    def test_resolves_device_project_to_user_machine(self):
        self.activate("ui-inspector")
        uid, ws = require_device_workspace()
        self.assertEqual((uid, str(ws)), (7, USER_DIR))
        self.assertEqual(agent_tools._remote_uid(), 7)

    def test_no_project_refuses_instead_of_workspace_root(self):
        with self.assertRaises(WorkspaceAccessDenied):
            agent_tools.active_workspace()

    def test_missing_device_header_does_not_resolve(self):
        # project active on the real device, request arrives as "default"
        self.activate("ui-inspector")
        set_current_device(None)
        with self.assertRaises(WorkspaceAccessDenied):
            require_device_workspace()

    def test_default_device_fallback_not_used(self):
        # the old get_active_project() fell back to "uid:default" -- must not
        self.activate("ui-inspector", did="default")
        with self.assertRaises(WorkspaceAccessDenied):
            require_device_workspace()

    def test_project_without_local_folder_refused(self):
        self.activate("sandbox")
        with self.assertRaisesRegex(WorkspaceAccessDenied, "no folder on your machine"):
            require_device_workspace()

    def test_companion_offline_refused(self):
        self.activate("ui-inspector")
        self.connected.clear()
        with self.assertRaisesRegex(WorkspaceAccessDenied, "Companion"):
            require_device_workspace()

    def test_other_users_project_name_not_resolved(self):
        self.activate("other")   # name exists, but only for user 8
        with self.assertRaises(WorkspaceAccessDenied):
            require_device_workspace()

    def test_tools_surface_refusal_not_server_files(self):
        for tool, args in [(agent_tools.tool_list_files, {}),
                           (agent_tools.tool_read_file, {"path": "TEST/app.js"}),
                           (agent_tools.tool_grep, {"pattern": "x"}),
                           (agent_tools.tool_run_python, {"code": "print(1)"})]:
            with self.subTest(tool=tool.__name__), self.assertRaises(WorkspaceAccessDenied):
                asyncio.run(tool(args))

    def test_label_is_none_when_unavailable(self):
        self.assertIsNone(agent_tools.workspace_label())

    def test_memory_does_not_index_server_workspace_by_default(self):
        self.assertEqual(asyncio.run(memory.index_workspace()), {"files": 0, "chunks": 0})


class ClientPathTests(unittest.TestCase):
    """Project folders are validated as text only -- nothing is created on the server."""

    def test_accepts_windows_posix_and_unc(self):
        from core.db import _client_workspace_path
        for p in (USER_DIR, "/home/[PLACEHOLDER]/code", r"\\fileserver\share\proj"):
            self.assertEqual(_client_workspace_path(p), p)

    def test_rejects_blank_relative_and_traversal(self):
        from core.db import _client_workspace_path
        for p in ("", "   ", "relative\\dir", r"C:\Users\x\..\..\Windows"):
            with self.subTest(p=p), self.assertRaises(ValueError):
                _client_workspace_path(p)

    def test_no_filesystem_calls_on_server(self):
        from core.db import _client_workspace_path
        with mock.patch("pathlib.Path.mkdir") as mk, mock.patch("pathlib.Path.resolve") as rs, \
                mock.patch("pathlib.Path.exists") as ex:
            _client_workspace_path(USER_DIR)
        mk.assert_not_called(); rs.assert_not_called(); ex.assert_not_called()

    def test_no_workspace_root_left(self):
        from core import small_model
        self.assertFalse(hasattr(small_model, "WORKSPACE_ROOT"))


if __name__ == "__main__":
    unittest.main()
