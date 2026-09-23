"""tests/test_agent_diff.py - Agent Task writes: per-call diffs, working file tree, no download badges.

Run: python -m unittest tests.test_agent_diff -v
"""

import asyncio
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import agent_tools, companion_bridge
from core.request_context import set_current_user, set_current_device

USER_DIR = r"C:\Users\[PLACEHOLDER]\projects\ui-inspector"


class FakeCompanion:
    """In-memory stand-in for the user's machine (fs.read / fs.write / fs.edit / fs.tree)."""

    def __init__(self):
        self.files = {}
        self.calls = []

    async def call(self, uid, op, params, timeout=None):
        self.calls.append(op)
        if op == "fs.read":
            return {"content": self.files.get(params["path"])}
        if op == "fs.write":
            existed = params["path"] in self.files
            prev = self.files.get(params["path"], "")
            self.files[params["path"]] = prev + params["content"] if params.get("append") else params["content"]
            return {"existed": existed}
        if op == "fs.edit":
            text = self.files[params["path"]]
            n = text.count(params["old_string"])
            self.files[params["path"]] = text.replace(params["old_string"], params["new_string"])
            return {"count": n}
        if op == "fs.tree":
            return {"nodes": [{"name": "AGENTS.md", "path": "AGENTS.md", "dir": False, "size": 10}]}
        raise RuntimeError(f"unknown op: {op}")


class AgentWriteTests(unittest.TestCase):
    def setUp(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE projects (name TEXT, user_id INTEGER, device_id TEXT, workspace_dir TEXT)")
        db.execute("INSERT INTO projects VALUES ('ui-inspector', 7, 'dev_laptop', ?)", (USER_DIR,))
        self.fc = FakeCompanion()
        self._patches = [
            mock.patch.object(agent_tools, "_projects_db", db),
            mock.patch.object(companion_bridge, "is_connected", lambda uid: uid == 7),
            mock.patch.object(companion_bridge, "call", self.fc.call),
            mock.patch.dict(agent_tools._active_project, {"7:dev_laptop": "ui-inspector"}, clear=True),
            mock.patch.dict(agent_tools._ws_changes, {}, clear=True),
            mock.patch.dict(agent_tools._file_diffs, {}, clear=True),
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

    def run_(self, coro):
        return asyncio.run(coro)

    def test_create_is_all_additions(self):
        self.run_(agent_tools.tool_write_file({"path": "AGENTS.md", "content": "# A\nline 2\n"}))
        d = agent_tools.pop_file_diff({"path": "AGENTS.md"})
        self.assertTrue(d["created"])
        self.assertEqual((d["added"], d["removed"]), (2, 0))
        self.assertEqual([h["t"] for h in d["hunks"] if h["t"] != "@"], ["+", "+"])
        self.assertIsNone(agent_tools.pop_file_diff({"path": "AGENTS.md"}), "diff is handed out once")

    def test_overwrite_and_edit_report_changed_lines(self):
        self.run_(agent_tools.tool_write_file({"path": "a.js", "content": "one\ntwo\nthree\n"}))
        agent_tools.pop_file_diff({"path": "a.js"})
        self.run_(agent_tools.tool_edit_file({"path": "a.js", "old_string": "two", "new_string": "TWO\nextra"}))
        d = agent_tools.pop_file_diff({"path": "a.js"})
        self.assertFalse(d["created"])
        self.assertEqual((d["added"], d["removed"]), (2, 1))
        # session baseline for the workspace panel keeps the original "before"
        rec = agent_tools._ws_changes[7][str(Path(USER_DIR) / "a.js")]
        self.assertIsNone(rec["before"])
        self.assertIn("TWO", rec["after"])

    def test_append_diff_counts_only_new_lines(self):
        self.run_(agent_tools.tool_write_file({"path": "b.txt", "content": "x\n"}))
        agent_tools.pop_file_diff({"path": "b.txt"})
        self.run_(agent_tools.tool_write_file({"path": "b.txt", "content": "y\n", "append": True}))
        d = agent_tools.pop_file_diff({"path": "b.txt"})
        self.assertEqual((d["added"], d["removed"]), (1, 0))

    def test_large_diff_truncated(self):
        body = "\n".join(f"l{i}" for i in range(1000))
        self.run_(agent_tools.tool_write_file({"path": "big.txt", "content": body}))
        d = agent_tools.pop_file_diff({"path": "big.txt"})
        self.assertTrue(d["truncated"])
        self.assertEqual(d["added"], 1000)
        self.assertLessEqual(len(d["hunks"]), agent_tools.MAX_DIFF_LINES)

    def test_revert_goes_through_companion_not_server_disk(self):
        target = str(Path(USER_DIR) / "a.js")
        self.fc.files[target] = "orig\n"
        self.run_(agent_tools.tool_write_file({"path": "a.js", "content": "changed\n"}))
        with mock.patch("pathlib.Path.write_text") as wt, mock.patch("pathlib.Path.unlink") as ul:
            out = self.run_(agent_tools.tool_revert({"path": "a.js"}))
        self.assertIn("reverted", out)
        self.assertEqual(self.fc.files[target], "orig\n")
        wt.assert_not_called(); ul.assert_not_called()

    def test_ws_tree_scan_with_tracked_changes(self):
        # regression: _ws_changes is keyed by user id; the tree used to call
        # .replace() on those int keys and 500 after the first write
        from routes import agent as agent_route
        self.run_(agent_tools.tool_write_file({"path": "AGENTS.md", "content": "hi"}))
        nodes = self.run_(agent_route._ws_tree_scan(""))
        self.assertEqual(nodes[0]["name"], "AGENTS.md")
        self.assertTrue(nodes[0]["changed"])


class DownloadMarkerTests(unittest.TestCase):
    def test_markers_removed_and_flagged(self):
        from routes.agent import _strip_download_markers
        text, synth = _strip_download_markers("Done.\n\n[DOWNLOAD: AGENTS.md]", False)
        self.assertEqual(text, "Done.")
        self.assertTrue(synth)

    def test_plain_text_untouched(self):
        from routes.agent import _strip_download_markers
        self.assertEqual(_strip_download_markers("All good", False), ("All good", False))


if __name__ == "__main__":
    unittest.main()
