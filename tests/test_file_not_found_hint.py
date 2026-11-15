"""A retyped (wrong) generated file name must lead back to the real file, never to a new one or a different run."""
import asyncio
import unittest
from pathlib import PureWindowsPath
from unittest import mock

from core.agent_tools import file_ops, file_state
from core.agent_tools.name_recovery import match_written

FOLDER = PureWindowsPath(r"D:\project_folder\agent-task\system-health")


class FakeCompanion:
    """In-memory device folder: fs.read / fs.list / fs.write; any other op (e.g. python.run) fails the test."""

    def __init__(self, files):
        self.files = dict(files)          # name -> text
        self.ops = []

    async def call(self, uid, op, args, **kw):
        self.ops.append(op)
        if op == "fs.read":
            return {"content": self.files.get(PureWindowsPath(args["path"]).name)}
        if op == "fs.list":
            return {"files": list(self.files)}
        if op == "fs.write":
            self.files[PureWindowsPath(args["path"]).name] = args["content"]
            return {"existed": False}
        raise AssertionError(f"unexpected companion op: {op}")


class MatchWritten(unittest.TestCase):
    def m(self, asked, written):
        got = match_written(FOLDER / asked, [str(FOLDER / w) for w in written])
        return got.name if got else None

    def test_the_names_from_the_failed_runs_resolve_to_the_real_file(self):
        self.assertEqual(self.m("report_cb1170f6a.html", ["report_cb1170f6.html"]), "report_cb1170f6.html")
        self.assertEqual(self.m("system-health-report_4a166a3a.html", ["system-health-report_4a16643a.html"]),
                         "system-health-report_4a16643a.html")
        self.assertEqual(self.m("system-health-report_4a1a643a.html", ["system-health-report_4a16643a.html"]),
                         "system-health-report_4a16643a.html")
        self.assertEqual(self.m("collect_events_22b31c54.py", ["collect_events_22b31c74.py"]),
                         "collect_events_22b31c74.py")

    def test_a_truncated_name_without_extension_resolves(self):
        self.assertEqual(self.m("analyze_system_928a0", ["analyze_system_928a0134.py"]), "analyze_system_928a0134.py")

    def test_ambiguous_unrelated_other_folder_or_other_extension_is_not_guessed(self):
        self.assertIsNone(self.m("analyze_system_928a0", ["analyze_system_928a0134.py", "analyze_system_928a0999.py"]))
        self.assertIsNone(self.m("notes.md", ["report_cb1170f6.html"]))
        self.assertIsNone(self.m("collect_events_22b31c54.txt", ["collect_events_22b31c74.py"]))
        other = match_written(FOLDER / "report_cb1170f6a.html", [r"D:\elsewhere\report_cb1170f6.html"])
        self.assertIsNone(other)


class ToolBehaviour(unittest.TestCase):
    def setUp(self):
        file_state.reset_session()
        self.addCleanup(file_state.reset_session)

    def _run(self, coro, files, written=()):
        fake = FakeCompanion(files)
        for w in written:
            file_state.mark_written(str(FOLDER / w))
        with mock.patch.object(file_ops, "_resolve", lambda a: (7, FOLDER / a)), \
                mock.patch.object(file_ops, "_cb", lambda: fake), \
                mock.patch("core.agent_tools.require_device_workspace", lambda: (7, FOLDER)), \
                mock.patch.object(file_ops, "require_device_workspace", lambda: (7, FOLDER)):
            return asyncio.run(coro), fake

    def test_read_of_a_mistyped_name_reads_the_file_we_wrote_and_says_so(self):
        out, _ = self._run(file_ops.tool_read_file({"path": "system-health-report_4a166a3a.html"}),
                           {"system-health-report_4a16643a.html": "<h1>hi</h1>"},
                           written=["system-health-report_4a16643a.html"])
        self.assertIn("system-health-report_4a16643a.html", out)
        self.assertIn("<h1>hi</h1>", out)
        self.assertIn("exact name", out)

    def test_read_does_not_auto_open_a_file_we_did_not_write(self):
        out, _ = self._run(file_ops.tool_read_file({"path": "system-health-report_4a166a3a.html"}),
                           {"system-health-report_4a16643a.html": "<h1>hi</h1>"})
        self.assertNotIn("<h1>hi</h1>", out)
        self.assertIn("system-health-report_4a16643a.html", out)      # offered as a name, not opened
        self.assertNotIn("write_file", out)

    def test_read_with_no_close_name_still_offers_write_file(self):
        out, _ = self._run(file_ops.tool_read_file({"path": "zzz_unrelated_name.html"}), {"a.html": "x"})
        self.assertIn("write_file", out)

    def test_append_points_at_the_real_name_and_does_not_create(self):
        out, fake = self._run(file_ops.tool_append_file({"path": "system-health-report_4a166a3a.html", "content": "x"}),
                              {"system-health-report_4a16643a.html": "<h1>hi</h1>"},
                              written=["system-health-report_4a16643a.html"])
        self.assertIn("system-health-report_4a16643a.html", out)
        self.assertNotIn("write_file", out)
        self.assertNotIn("fs.write", fake.ops)

    def test_edit_of_a_mistyped_name_reports_not_found_not_read_first(self):
        out, _ = self._run(file_ops.tool_edit_file({"path": "collect_events_22b31c54.py", "old_string": "a",
                                                    "new_string": "b"}),
                           {"collect_events_22b31c74.py": "a"}, written=["collect_events_22b31c74.py"])
        self.assertIn("collect_events_22b31c74.py", out)
        self.assertNotIn("read collect_events", out)

    def test_run_python_with_a_truncated_name_names_the_script_and_never_runs_it(self):
        out, fake = self._run(file_ops.tool_run_python({"path": "analyze_system_928a0"}),
                              {"analyze_system_928a0134.py": "print(1)"}, written=["analyze_system_928a0134.py"])
        self.assertIn("analyze_system_928a0134.py", out)
        self.assertNotIn("python.run", fake.ops)      # approval covers the exact call: a different script never runs

    def test_run_python_with_a_mistyped_py_name_names_the_script_and_never_runs_it(self):
        out, fake = self._run(file_ops.tool_run_python({"path": "collect_events_22b31c54.py"}),
                              {"collect_events_22b31c74.py": "print(1)"}, written=["collect_events_22b31c74.py"])
        self.assertIn("collect_events_22b31c74.py", out)
        self.assertNotIn("python.run", fake.ops)


if __name__ == "__main__":
    unittest.main()
