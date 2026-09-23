"""tests/test_companion_bridge.py - riding out companion reconnects; read_file_chunk stays off server disk.

Run: python -m unittest tests.test_companion_bridge -v
"""

import asyncio
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import agent_tools, companion_bridge as cb, file_tools
from core.request_context import set_current_user, set_current_device


class FakeSocket:
    """Answers each call frame via the bridge's pending futures, like the companion would."""

    def __init__(self, uid, answer):
        self.uid, self.answer, self.sent = uid, answer, []

    async def send_text(self, raw):
        import json
        frame = json.loads(raw)
        self.sent.append(frame["op"])
        res = self.answer(frame)
        if res is not None:
            cb._pending[self.uid][frame["req_id"]].set_result(res)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self._patches = [mock.patch.dict(cb._connections, {}, clear=True),
                         mock.patch.dict(cb._pending, {}, clear=True),
                         mock.patch.dict(cb._last_seen, {}, clear=True),
                         mock.patch.object(cb, "CONNECT_WAIT_S", 1.0)]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def test_waits_for_a_late_reconnect(self):
        ok = FakeSocket(1, lambda f: {"ok": True, "data": {"content": "hi"}})

        async def scenario():
            async def reconnect():
                await asyncio.sleep(0.3)
                cb._connections[1] = ok
                cb._pending.setdefault(1, {})
            asyncio.create_task(reconnect())
            return await cb.call(1, "fs.read", {"path": "x"})
        self.assertEqual(asyncio.run(scenario()), {"content": "hi"})

    def test_gives_up_when_companion_stays_away(self):
        with self.assertRaises(ConnectionError):
            asyncio.run(cb.call(1, "fs.read", {"path": "x"}))

    def _dropping_socket(self, uid, drops):
        """First `drops` calls die mid-flight (companion disconnected), then answer."""
        state = {"n": 0}

        def answer(frame):
            state["n"] += 1
            if state["n"] <= drops:
                cb._pending[uid][frame["req_id"]].set_exception(ConnectionError("interrupted"))
                return None
            return {"ok": True, "data": {"files": ["a.js"]}}
        return FakeSocket(uid, answer)

    def test_read_only_op_retried_once(self):
        sock = self._dropping_socket(1, drops=1)
        cb._connections[1] = sock
        cb._pending[1] = {}
        self.assertEqual(asyncio.run(cb.call(1, "fs.list", {})), {"files": ["a.js"]})
        self.assertEqual(sock.sent, ["fs.list", "fs.list"])

    def test_write_never_retried(self):
        sock = self._dropping_socket(1, drops=1)
        cb._connections[1] = sock
        cb._pending[1] = {}
        with self.assertRaises(ConnectionError):
            asyncio.run(cb.call(1, "fs.write", {"path": "a", "content": "x"}))
        self.assertEqual(sock.sent, ["fs.write"])

    def test_availability_grace_window(self):
        self.assertFalse(cb.is_available(1))
        cb._last_seen[1] = time.time()
        self.assertTrue(cb.is_available(1))
        cb._last_seen[1] = time.time() - 60
        self.assertFalse(cb.is_available(1))
        cb._connections[1] = object()
        self.assertTrue(cb.is_available(1))


USER_DIR = r"C:\Users\[PLACEHOLDER]\projects\news"


class ReadChunkTests(unittest.TestCase):
    def setUp(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE projects (name TEXT, user_id INTEGER, device_id TEXT, workspace_dir TEXT)")
        db.execute("INSERT INTO projects VALUES ('news', 7, 'dev_laptop', ?)", (USER_DIR,))
        self.files = {str(Path(USER_DIR) / "background.js"): "x" * 25}
        self.tmp = tempfile.TemporaryDirectory()

        async def fake_call(uid, op, params, timeout=None):
            assert op == "fs.read"
            return {"content": self.files.get(params["path"])}
        self._patches = [
            mock.patch.object(agent_tools, "_projects_db", db),
            mock.patch.object(cb, "is_connected", lambda uid: uid == 7),
            mock.patch.object(cb, "call", fake_call),
            mock.patch.dict(agent_tools._active_project, {"7:dev_laptop": "news"}, clear=True),
            mock.patch.object(agent_tools, "COMMON_ROOT", Path(self.tmp.name)),
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
        self.tmp.cleanup()

    def test_project_file_read_through_companion_only(self):
        real_is_file = Path.is_file
        project_touched = []

        def guarded_is_file(p):
            if str(p).startswith(USER_DIR):
                project_touched.append(str(p))
            return real_is_file(p)
        with mock.patch("pathlib.Path.is_file", guarded_is_file), \
                mock.patch("pathlib.Path.read_text", side_effect=AssertionError("server disk read")):
            out = asyncio.run(file_tools.tool_read_file_chunk({"path": "background.js", "offset_chars": 10, "max_chars": 5}))
        self.assertTrue(out.startswith("xxxxx"))
        self.assertIn("chars 10-14 of 25", out)
        self.assertEqual(project_touched, [], "never stat the project path on the server")

    def test_uploaded_attachment_still_served_from_common_space(self):
        (Path(self.tmp.name) / "notes-1a2b3c4d.txt").write_text("uploaded text", encoding="utf-8")
        out = asyncio.run(file_tools.tool_read_file_chunk({"path": "notes-1a2b3c4d.txt"}))
        self.assertTrue(out.startswith("uploaded text"))

    def test_missing_everywhere(self):
        out = asyncio.run(file_tools.tool_read_file_chunk({"path": "nope.js"}))
        self.assertIn("File not found", out)


if __name__ == "__main__":
    unittest.main()
