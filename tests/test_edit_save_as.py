import asyncio
import unittest
from pathlib import Path
from unittest import mock

from core.agent_tools import file_ops

ORIGINAL = "<html>\n<script>const a = 1;</script>\n<p>charts</p>\n</html>\n"


class EditSaveAs(unittest.TestCase):
    def setUp(self):
        self.mem = {"dash.html": ORIGINAL}
        self.patches = [
            mock.patch.object(file_ops, "_resolve", side_effect=lambda a: (1, Path(a))),
            mock.patch.object(file_ops, "_read_existing", side_effect=self._read),
            mock.patch.object(file_ops, "_store", side_effect=self._store),
            mock.patch.object(file_ops.file_state, "was_read", return_value=True),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    async def _read(self, uid, p):
        return self.mem.get(str(p))

    async def _store(self, uid, p, before, after):
        self.mem[str(p)] = after

    def run_edit(self, args):
        return asyncio.run(file_ops._edit_file(args))

    def test_save_as_keeps_the_original_and_writes_the_copy(self):
        out = self.run_edit({"path": "dash.html", "old_string": "const a = 1;", "new_string": "const a = 2;",
                             "save_as": "dash-v2.html", "skip_verify": True, "pre_save_check": False})
        self.assertIn("[DOWNLOAD: dash-v2.html]", out)
        self.assertEqual(self.mem["dash.html"], ORIGINAL)
        self.assertIn("const a = 2;", self.mem["dash-v2.html"])
        self.assertIn("<p>charts</p>", self.mem["dash-v2.html"])     # the rest of the file is untouched

    def test_save_as_refuses_an_existing_name(self):
        self.mem["dash-v2.html"] = "old version"
        out = self.run_edit({"path": "dash.html", "old_string": "charts", "new_string": "graphs",
                             "save_as": "dash-v2.html", "skip_verify": True, "pre_save_check": False})
        self.assertTrue(out.startswith("error:"), out)
        self.assertEqual(self.mem["dash-v2.html"], "old version")

    def test_without_save_as_the_file_is_edited_in_place(self):
        self.run_edit({"path": "dash.html", "old_string": "charts", "new_string": "graphs",
                       "skip_verify": True, "pre_save_check": False})
        self.assertIn("<p>graphs</p>", self.mem["dash.html"])


if __name__ == "__main__":
    unittest.main()
