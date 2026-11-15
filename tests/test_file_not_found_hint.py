"""A missing file must point at the real close names in the folder, never send the agent to create a new one."""
import asyncio
import unittest
from pathlib import Path
from unittest import mock

from core.agent_tools import file_ops

FOLDER = Path(r"D:\project_folder\agent-task\system-health")
REAL = "system-health-report_4a16643a.html"


class FakeCompanion:
    def __init__(self, files):
        self.files = files

    async def call(self, uid, op, args):
        if op == "fs.read":
            return {"content": None}
        if op == "fs.list":
            return {"files": self.files}
        raise AssertionError(op)


class NotFoundHints(unittest.TestCase):
    def _run(self, coro, files):
        fake = FakeCompanion(files)
        with mock.patch.object(file_ops, "_resolve", lambda a: (7, FOLDER / a)), \
                mock.patch.object(file_ops, "_cb", lambda: fake):
            return asyncio.run(coro)

    def test_read_points_at_the_close_real_name(self):
        out = self._run(file_ops.tool_read_file({"path": "system-health-report_4a166a3a.html"}), [REAL, "other.py"])
        self.assertIn(REAL, out)
        self.assertNotIn("write_file", out)

    def test_read_with_no_close_name_still_offers_write_file(self):
        out = self._run(file_ops.tool_read_file({"path": "zzz_unrelated_name.html"}), [REAL])
        self.assertIn("write_file", out)

    def test_append_points_at_the_close_real_name_and_does_not_create(self):
        out = self._run(file_ops.tool_append_file({"path": "system-health-report_4a166a3a.html", "content": "x"}), [REAL])
        self.assertIn(REAL, out)
        self.assertNotIn("write_file", out)


if __name__ == "__main__":
    unittest.main()
