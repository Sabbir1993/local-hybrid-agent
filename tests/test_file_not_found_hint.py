"""A retyped (wrong) generated file name must lead back to the real file, never to a new one or a different run.

A Personal Agent's write_file is renamed by the server to '<name>_<8 hex>.<ext>', so the model has to copy a random id.
Read-only and write tools recover from a wrong copy; run_python only names the real script (approval is per exact call)."""
import asyncio
import unittest
from pathlib import PureWindowsPath
from unittest import mock

from core.agent_tools import diffs, file_ops, file_state, verify_loop
from core.agent_tools.name_recovery import match_written

FOLDER = PureWindowsPath(r"D:\project_folder\agent-task\system-health")
REAL = "system_report_aeedb236.html"


class FakeCompanion:
    """In-memory device folder: fs.read / fs.list / fs.write; any other op is recorded and fails like an old companion."""

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
        raise RuntimeError(f"unknown op: {op}")


class MatchWritten(unittest.TestCase):
    def m(self, asked, written):
        got = match_written(FOLDER / asked, [str(FOLDER / w) for w in written])
        return got.name if got else None

    def test_the_names_from_the_failed_runs_resolve_to_the_real_file(self):
        self.assertEqual(self.m("report_cb1170f6a.html", ["report_cb1170f6.html"]), "report_cb1170f6.html")
        self.assertEqual(self.m("system-health-report_4a166a3a.html", ["system-health-report_4a16643a.html"]),
                         "system-health-report_4a16643a.html")
        self.assertEqual(self.m("collect_events_22b31c54.py", ["collect_events_22b31c74.py"]),
                         "collect_events_22b31c74.py")

    def test_every_retyped_variant_of_the_looping_run_resolves(self):
        for asked in ("system_report_aeed236.html", "system_report_aeedb23.html", "system_report_aeedb2.html",
                      "system_report_aeedb26.html", "system_report_aeedb36.html", "system_report_aeedb2",
                      "system_report_aeedb"):
            with self.subTest(asked=asked):
                self.assertEqual(self.m(asked, [REAL]), REAL)

    def test_a_truncated_name_without_extension_resolves(self):
        self.assertEqual(self.m("analyze_system_928a0", ["analyze_system_928a0134.py"]), "analyze_system_928a0134.py")

    def test_the_name_the_model_chose_resolves_to_the_newest_server_rename_of_it(self):
        old, new = "system_report_aaaa1111.html", "system_report_bbbb2222.html"
        self.assertEqual(self.m("system_report.html", [old, new]), new)
        self.assertEqual(self.m("system_report.html", [new, old]), old)       # newest = last written
        self.assertEqual(self.m("system_report", [old, new]), new)             # extension left off
        self.assertIsNone(self.m("system_report.pdf", [old, new]))             # a different extension is a different file

    def test_ambiguous_unrelated_other_folder_or_other_extension_is_not_guessed(self):
        self.assertIsNone(self.m("analyze_system_928a0", ["analyze_system_928a0134.py", "analyze_system_928a0999.py"]))
        self.assertIsNone(self.m("notes.md", ["report_cb1170f6.html"]))
        self.assertIsNone(self.m("collect_events_22b31c54.txt", ["collect_events_22b31c74.py"]))
        self.assertIsNone(match_written(FOLDER / "report_cb1170f6a.html", [r"D:\elsewhere\report_cb1170f6.html"]))


class ToolBehaviour(unittest.TestCase):
    def setUp(self):
        file_state.reset_session()
        self.addCleanup(file_state.reset_session)

    def _run(self, coro_fn, args, files, written=()):
        fake = FakeCompanion(files)
        for w in written:                              # what _store does after a write_file
            file_state.mark_written(str(FOLDER / w))
            file_state.mark_read(str(FOLDER / w))
        with mock.patch.object(file_ops, "_resolve", lambda a: (7, FOLDER / a)), \
                mock.patch.object(file_ops, "_cb", lambda: fake), \
                mock.patch.object(verify_loop, "_cb", lambda: fake), \
                mock.patch("core.agent_tools.require_device_workspace", lambda: (7, FOLDER)), \
                mock.patch.object(file_ops, "require_device_workspace", lambda: (7, FOLDER)):
            return asyncio.run(coro_fn(args)), fake

    def test_read_of_a_mistyped_name_reads_the_file_we_wrote_and_says_so(self):
        out, _ = self._run(file_ops.tool_read_file, {"path": "system_report_aeed236.html"},
                           {REAL: "<h1>hi</h1>"}, written=[REAL])
        self.assertIn(REAL, out)
        self.assertIn("<h1>hi</h1>", out)
        self.assertIn("exact name", out)

    def test_read_does_not_auto_open_a_file_we_did_not_write(self):
        out, _ = self._run(file_ops.tool_read_file, {"path": "system_report_aeed236.html"}, {REAL: "<h1>hi</h1>"})
        self.assertNotIn("<h1>hi</h1>", out)
        self.assertIn(REAL, out)                                   # offered as a name, not opened
        self.assertNotIn("write_file", out)

    def test_read_with_no_close_name_warns_before_creating_a_new_file(self):
        out, _ = self._run(file_ops.tool_read_file, {"path": "zzz_unrelated_name.html"}, {"a.html": "x"})
        self.assertIn("Do not create a new file", out)

    def test_append_to_a_mistyped_name_lands_on_the_file_we_wrote(self):
        out, fake = self._run(file_ops.tool_append_file, {"path": "system_report_aeed236.html", "content": "<p>2</p>"},
                              {REAL: "<h1>hi</h1>"}, written=[REAL])
        self.assertIn(REAL, out)
        self.assertIn("exact name", out)
        self.assertEqual(set(fake.files), {REAL})                  # no new file was created
        self.assertIn("<p>2</p>", fake.files[REAL])
        self.assertIn("<h1>hi</h1>", fake.files[REAL])
        self.assertEqual(file_state.alias_target(str(FOLDER / "system_report_aeed236.html")), str(FOLDER / REAL))

    def test_append_to_the_name_the_model_chose_lands_on_the_server_renamed_file(self):
        _, fake = self._run(file_ops.tool_append_file, {"path": "system_report.html", "content": "<p>2</p>"},
                            {REAL: "<h1>hi</h1>"}, written=[REAL])
        self.assertEqual(set(fake.files), {REAL})
        self.assertIn("<p>2</p>", fake.files[REAL])

    def test_append_never_writes_to_a_file_we_did_not_write(self):
        out, fake = self._run(file_ops.tool_append_file, {"path": "system_report_aeed236.html", "content": "x"},
                              {REAL: "<h1>hi</h1>"})
        self.assertIn(REAL, out)                                   # named as a hint only
        self.assertNotIn("write_file", out)
        self.assertNotIn("fs.write", fake.ops)
        self.assertEqual(fake.files[REAL], "<h1>hi</h1>")

    def test_edit_of_a_mistyped_name_edits_the_file_we_wrote(self):
        out, fake = self._run(file_ops.tool_edit_file, {"path": "system_report_aeedb23.html", "old_string": "hi",
                                                        "new_string": "there"},
                              {REAL: "<h1>hi</h1>"}, written=[REAL])
        self.assertIn(REAL, out)
        self.assertEqual(fake.files[REAL], "<h1>there</h1>")

    def test_edit_of_a_mistyped_name_for_a_file_we_did_not_write_reports_not_found(self):
        out, fake = self._run(file_ops.tool_edit_file, {"path": "system_report_aeedb23.html", "old_string": "hi",
                                                        "new_string": "there"}, {REAL: "<h1>hi</h1>"})
        self.assertIn(REAL, out)
        self.assertNotIn("read system_report", out)
        self.assertEqual(fake.files[REAL], "<h1>hi</h1>")

    def test_the_live_diff_is_found_through_the_alias(self):
        self._run(file_ops.tool_append_file, {"path": "system_report_aeed236.html", "content": "<p>2</p>"},
                  {REAL: "<h1>hi</h1>"}, written=[REAL])
        with mock.patch.object(diffs, "_ws_resolve", lambda a: FOLDER / a), \
                mock.patch("core.agent_tools._ws_resolve", lambda a: FOLDER / a, create=True):
            d = diffs.pop_file_diff({"path": "system_report_aeed236.html"})
        self.assertIsNotNone(d)

    def test_run_python_with_a_truncated_name_names_the_script_and_never_runs_it(self):
        out, fake = self._run(file_ops.tool_run_python, {"path": "analyze_system_928a0"},
                              {"analyze_system_928a0134.py": "print(1)"}, written=["analyze_system_928a0134.py"])
        self.assertIn("analyze_system_928a0134.py", out)
        self.assertNotIn("python.run", fake.ops)       # approval covers the exact call: a different script never runs

    def test_run_python_with_a_mistyped_py_name_names_the_script_and_never_runs_it(self):
        out, fake = self._run(file_ops.tool_run_python, {"path": "collect_events_22b31c54.py"},
                              {"collect_events_22b31c74.py": "print(1)"}, written=["collect_events_22b31c74.py"])
        self.assertIn("collect_events_22b31c74.py", out)
        self.assertNotIn("python.run", fake.ops)


class PersonalAgentEndToEnd(unittest.TestCase):
    """The real run_tool path of a Personal Agent. write_file keeps the model's name (no random-id rename)."""

    def setUp(self):
        from core import request_context as rc
        file_state.reset_session()
        self.addCleanup(file_state.reset_session)
        self.fake = FakeCompanion({})
        tok = rc.set_personal_scope(True)
        self.addCleanup(lambda: rc._personal_scope.reset(tok))

    def _go(self, coro_fn):
        with mock.patch("core.agent_tools.require_device_workspace", lambda: (7, FOLDER)), \
                mock.patch.object(file_ops, "_cb", lambda: self.fake), \
                mock.patch.object(verify_loop, "_cb", lambda: self.fake):
            return asyncio.run(coro_fn())

    def test_write_then_append_and_edit_by_the_chosen_or_a_mistyped_name_all_land_in_one_file(self):
        from core.agent_loop import execution

        async def go():
            await execution.run_tool("write_file", {"path": "system_report.html", "content": "<h1>hi</h1>"})
            await execution.run_tool("append_file", {"path": "system_report.html", "content": "<p>2</p>"})
            await execution.run_tool("append_file", {"path": "system_reprt.html", "content": "<p>3</p>"})
            await execution.run_tool("edit_file", {"path": "system_reprt.html", "old_string": "hi", "new_string": "there"})

        self._go(go)
        self.assertEqual(list(self.fake.files), ["system_report.html"])         # the name the model chose, one file
        self.assertEqual(self.fake.files["system_report.html"], "<h1>there</h1>\n<p>2</p>\n<p>3</p>")

    def test_write_file_never_overwrites_a_file_the_agent_did_not_create(self):
        from core.agent_loop import execution
        self.fake.files["report.html"] = "OLD"                                  # the user's own file

        async def go():
            return await execution.run_tool("write_file", {"path": "report.html", "content": "NEW", "overwrite": True})

        out = self._go(go)
        self.assertIn("report_2.html", out)                                      # a short free name, easy to copy
        self.assertEqual(self.fake.files["report.html"], "OLD")                  # untouched, even with overwrite=true
        self.assertNotIn("fs.write", self.fake.ops)

    def test_the_suggested_name_skips_names_already_taken(self):
        from core.agent_loop import execution
        self.fake.files.update({"report.html": "OLD", "report_2.html": "OLD2"})

        async def go():
            return await execution.run_tool("write_file", {"path": "report.html", "content": "NEW"})

        self.assertIn("report_3.html", self._go(go))

    def test_write_file_append_to_a_file_the_agent_did_not_create_is_refused(self):
        from core.agent_loop import execution
        self.fake.files["report.html"] = "OLD"

        async def go():
            return await execution.run_tool("write_file", {"path": "report.html", "content": "NEW", "append": True})

        self.assertIn("already exists", self._go(go))
        self.assertEqual(self.fake.files["report.html"], "OLD")

    def test_the_agent_may_rewrite_its_own_file_with_overwrite(self):
        from core.agent_loop import execution

        async def go():
            await execution.run_tool("write_file", {"path": "mine.html", "content": "v1"})
            blocked = await execution.run_tool("write_file", {"path": "mine.html", "content": "v2"})
            await execution.run_tool("write_file", {"path": "mine.html", "content": "v2", "overwrite": True})
            return blocked

        blocked = self._go(go)
        self.assertIn("overwrote", blocked)      # the agent may rewrite a file it created itself
        self.assertNotIn("error:", blocked)
        self.assertEqual(self.fake.files["mine.html"], "v2")

    def test_editing_an_old_file_does_not_make_it_overwritable(self):
        from core.agent_loop import execution
        self.fake.files["report.html"] = "<h1>hi</h1>"

        async def go():
            await execution.run_tool("read_file", {"path": "report.html"})
            await execution.run_tool("edit_file", {"path": "report.html", "old_string": "hi", "new_string": "yo"})
            return await execution.run_tool("write_file", {"path": "report.html", "content": "NEW", "overwrite": True})

        out = self._go(go)
        self.assertEqual(self.fake.files["report.html"], "<h1>yo</h1>")          # edit_file worked, write_file refused
        self.assertIn("report_2.html", out)

    def test_agent_task_mode_keeps_its_own_rules(self):
        from core import request_context as rc
        from core.agent_loop import execution
        self.fake.files["report.html"] = "OLD"
        rc._personal_scope.set(False)

        async def go():
            return await execution.run_tool("write_file", {"path": "report.html", "content": "NEW"})

        out = self._go(go)
        self.assertIn("already exists", out)
        self.assertNotIn("not created in this session", out)                     # the Personal-only rule is not applied


if __name__ == "__main__":
    unittest.main()
