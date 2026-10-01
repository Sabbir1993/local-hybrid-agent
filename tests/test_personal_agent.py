"""A Personal Agent (started from Chat) works in one folder on the user's machine that is set on the agent:
it can read, write and analyse there, run read-only commands, and is refused everything else."""
import asyncio
import unittest
from pathlib import Path
from unittest import mock

from core.agent_loop import execution
from core.agent_tools import workspace
from core.auth_db.custom_agents import clean_work_dir
from core.request_context import (PERSONAL_BLOCKED_TOOLS, personal_scope, personal_workspace,
                                  set_personal_scope, set_personal_workspace)
from core.shell_tools import personal_write_violation
from routes.agent.models import AgentRequest


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class WorkFolderTests(unittest.TestCase):
    def test_a_plain_folder_is_kept(self):
        self.assertEqual(clean_work_dir("D:\\reports\\weekly\\"), "D:\\reports\\weekly")

    def test_empty_means_no_folder(self):
        self.assertEqual(clean_work_dir("  "), "")
        self.assertEqual(clean_work_dir(None), "")

    def test_unsafe_folders_are_refused(self):
        for bad in ("C:\\", "D:", "reports\\weekly", "D:\\a\\..\\b", "C:\\Windows\\Temp",
                    "C:\\Program Files\\x", "C:\\Users\\me\\AppData\\Local\\x"):
            with self.assertRaises(ValueError, msg=bad):
                clean_work_dir(bad)


class ShellRuleTests(unittest.TestCase):
    def test_read_only_commands_pass(self):
        for c in ("dir", "type notes.txt", "whoami", "hostname", "ipconfig /all", "tasklist",
                  "git status", "git log --oneline", "systeminfo", "findstr /s todo *.md"):
            self.assertIsNone(personal_write_violation(c), c)

    def test_anything_that_writes_or_starts_a_program_is_refused(self):
        for c in ("echo hi > out.txt", "dir | tee out.txt", "del x.txt", "rm -rf build", "move a b",
                  "copy a b", "mkdir x", "npm install", "pip install x",
                  "git commit -m x", "git checkout main", "powershell -c gci", "cmd /c dir", "curl http://x",
                  "reg add HKCU\\x", "Set-Content a.txt hi", "sed -i s/a/b/ f"):
            self.assertIsNotNone(personal_write_violation(c), c)

    def test_wmic_where_comparison_is_not_a_redirect(self):
        ok = "wmic process where ProcessId > 0 get Name,ProcessId,WorkingSetSize,Threads,CommandLine"
        self.assertIsNone(personal_write_violation(ok))
        self.assertIsNotNone(personal_write_violation(ok + " > procs.txt"))
        self.assertIsNotNone(personal_write_violation("echo a > 0"))

    def test_powershell_cmdlets_that_start_with_a_blocked_word_pass(self):
        for c in ("Get-Process | Sort-Object CPU -Descending | Format-Table -AutoSize",
                  "Get-Service | Where-Object Status -eq Running | Select-Object -First 5 | Format-List",
                  "Get-CimInstance Win32_OperatingSystem | Select-Object Caption, Version"):
            self.assertIsNone(personal_write_violation(c), c)
        for c in ("format c:", "Copy-Item a b", "Remove-Item x", "Start-Process calc", "start calc"):
            self.assertIsNotNone(personal_write_violation(c), c)

    def test_discarding_output_is_fine_and_the_refusal_names_the_match(self):
        self.assertIsNone(personal_write_violation("tasklist 2>nul"))
        self.assertIsNone(personal_write_violation("dir 2>&1"))
        self.assertIsNotNone(personal_write_violation("dir > nul.txt"))
        self.assertIn("`net`", personal_write_violation("net user"))


class RunToolTests(unittest.TestCase):
    def setUp(self):
        from core import request_context as rc
        self.rc = rc
        self.tok = set_personal_scope(True)

    def tearDown(self):
        self.rc._personal_scope.reset(self.tok)

    def test_scope_is_on_inside_the_run(self):
        self.assertTrue(personal_scope())

    def test_code_execution_and_sub_agents_are_refused(self):
        self.assertEqual(PERSONAL_BLOCKED_TOOLS, {"spawn_agent", "spawn_parallel_agents"})
        for name in sorted(PERSONAL_BLOCKED_TOOLS):
            out = run(execution.run_tool(name, {"code": "print(1)"}))
            self.assertTrue(out.startswith("error:"), name)
            self.assertIn("Personal Agent", out)

    def test_shell_write_is_refused_before_it_reaches_the_companion(self):
        with mock.patch("core.registry.registry.async_run", side_effect=AssertionError("must not run")):
            out = run(execution.run_tool("run_shell", {"command": "echo x > a.txt"}))
        self.assertTrue(out.startswith("error:"))

    def test_file_tools_are_not_redirected(self):
        # writes happen in the agent's own folder through the normal, path-checked file tools
        with mock.patch("core.registry.registry.get", return_value=object()), \
             mock.patch("core.registry.registry.async_run", return_value="wrote 2 chars") as r:
            out = run(execution.run_tool("write_file", {"path": "a.txt", "content": "hi"}))
        self.assertEqual(out, "wrote 2 chars")
        r.assert_called_once()


class WorkspaceOverrideTests(unittest.TestCase):
    def test_personal_folder_replaces_the_project_folder(self):
        from core import request_context as rc
        tok = set_personal_workspace("D:\\reports\\weekly")
        try:
            with mock.patch.object(workspace, "get_current_user_id", return_value=7), \
                 mock.patch.object(workspace.companion_bridge, "is_available", return_value=True):
                uid, path = workspace.require_device_workspace()
        finally:
            rc._personal_workspace.reset(tok)
        self.assertEqual((uid, str(path)), (7, "D:\\reports\\weekly"))

    def test_needs_a_connected_companion(self):
        from core import request_context as rc
        tok = set_personal_workspace("D:\\reports\\weekly")
        try:
            with mock.patch.object(workspace, "get_current_user_id", return_value=7), \
                 mock.patch.object(workspace.companion_bridge, "is_available", return_value=False):
                with self.assertRaises(workspace.WorkspaceAccessDenied):
                    workspace.require_device_workspace()
        finally:
            rc._personal_workspace.reset(tok)

    def test_no_override_outside_a_personal_run(self):
        self.assertIsNone(personal_workspace())


class WiringTests(unittest.TestCase):
    def test_request_defaults_to_agent_task_behaviour(self):
        self.assertFalse(AgentRequest(messages=[]).personal)

    def test_run_route_needs_an_owned_agent_with_a_folder(self):
        # Setup guards moved to routes/agent/setup.py in the C1 setup extraction;
        # loop-gate strings stay in run.py. Both files are checked.
        setup_src = Path("routes/agent/setup.py").read_text(encoding="utf-8")
        self.assertIn("personal_needs_folder", setup_src)
        self.assertIn("set_personal_scope(ctx.personal)", setup_src)
        src = Path("routes/agent/run.py").read_text(encoding="utf-8")
        self.assertIn("not in hidden_tools", src)
        self.assertIn("(cfg.get(\"ask_first\", True) or personal)", src)
        self.assertIn("'saveable': False if personal", src)

    def test_client_sends_the_flag_only_from_chat(self):
        src = Path("static/js/agent-run.js").read_text(encoding="utf-8")
        self.assertIn("personal: !agentMode || undefined", src)


class CustomizeUiTests(unittest.TestCase):
    def test_agents_tab_and_disconnect_are_wired(self):
        html = Path("ui.html").read_text(encoding="utf-8")
        self.assertIn('data-kind="agents"', html)
        self.assertIn('id="ca-workdir-browse"', html)
        js = Path("static/js/customize.js").read_text(encoding="utf-8")
        for needle in ("renderAgents", "data-agent-approve", "work_dir: dir", "data-remove", "Disconnect"):
            self.assertIn(needle, js)

    def test_only_own_agents_are_listed_in_chat(self):
        js = Path("static/js/custom-agents.js").read_text(encoding="utf-8")
        self.assertNotIn("forkable(", js)
        self.assertIn("window.pickFolder", js)


if __name__ == "__main__":
    unittest.main()


class FileWritingNeverWithheldTests(unittest.TestCase):
    def test_restricted_agents_keep_the_file_write_tools(self):
        # The allowlist union moved to routes/agent/setup.py in the C1 setup extraction.
        setup_src = Path("routes/agent/setup.py").read_text(encoding="utf-8")
        self.assertIn('set(ctx.custom_agent["tool_allowlist"]) | {"write_file", "edit_file", "append_file"}', setup_src)
        chat_run = Path("routes/chat/run.py").read_text(encoding="utf-8")
        self.assertIn('set(custom_agent["tool_allowlist"]) | {"write_file"}', chat_run)
        builder = Path("static/js/custom-agents.js").read_text(encoding="utf-8")
        self.assertIn("always || selectedTools.includes(t.name)", builder)
        self.assertIn("ca-tool-on", builder)
        self.assertNotIn("always ? 'disabled'", builder)     # ticked and normal, not greyed out


class LoopFixTests(unittest.TestCase):
    def test_system_reporter_can_run_read_only_commands(self):
        from core.auth_db.custom_agents_seed import STARTER_CUSTOM_AGENTS
        rep = next(a for a in STARTER_CUSTOM_AGENTS if a["slug"] == "system-reporter")
        self.assertIn("run_shell", rep["tool_allowlist"])
        self.assertIn("run_shell", rep["system_prompt"])

    def test_blocked_tool_message_points_to_run_shell(self):
        tok = set_personal_scope(True)
        try:
            out = run(execution.run_tool("spawn_agent", {"task": "x"}))
        finally:
            from core import request_context as rc
            rc._personal_scope.reset(tok)
        self.assertIn("run_shell", out)
        self.assertIn("Do not retry 'spawn_agent'", out)

    def test_identical_lookups_are_not_rerun(self):
        src = Path("routes/agent/run.py").read_text(encoding="utf-8")
        self.assertIn("seen_reads", src)
        self.assertIn("already ran and its result has not changed", src)
        self.assertIn("seen_reads.clear()", src)


class AgentCardUiTests(unittest.TestCase):
    def test_filters_and_equal_buttons_exist(self):
        js = Path("static/js/custom-agents.js").read_text(encoding="utf-8")
        for needle in ('id="ca-tool-filter"', 'id="ca-grid-filter"', 'class="ca-act primary btn-settings-run"'):
            self.assertIn(needle, js)
        css = Path("static/css/style.css").read_text(encoding="utf-8")
        self.assertIn("grid-auto-columns: 1fr", css)


class PythonWithApprovalTests(unittest.TestCase):
    def test_python_and_node_one_liners_reach_the_approval_step(self):
        for c in ('python -c "print(1)"', "python -c \"print('python-ok')\"", "py -3 --version", "node -e \"console.log(1)\""):
            self.assertIsNone(personal_write_violation(c), c)

    def test_shells_and_installers_are_still_refused(self):
        for c in ("powershell -c gci", "cmd /c dir", "pip install x", "npm i", "python -c \"print(1)\" > out.txt"):
            self.assertIsNotNone(personal_write_violation(c), c)

    def test_run_python_always_asks_in_a_personal_run(self):
        src = Path("routes/agent/run.py").read_text(encoding="utf-8")
        self.assertIn('name == "run_python" and (shell_cfg().get("ask_first", True) or personal)', src)


class UniqueFileNameTests(unittest.TestCase):
    def test_name_gets_a_fresh_id_and_keeps_folder_and_extension(self):
        out = execution.unique_file_name("reports/weekly.md")
        self.assertRegex(out, r"^reports/weekly_[0-9a-f]{8}\.md$")

    def test_an_earlier_id_is_replaced_not_stacked(self):
        out = execution.unique_file_name("weekly_1a2b3c4d.md")
        self.assertRegex(out, r"^weekly_[0-9a-f]{8}\.md$")
        self.assertNotIn("1a2b3c4d", out)

    def test_two_creates_never_share_a_name(self):
        self.assertNotEqual(execution.unique_file_name("a.txt"), execution.unique_file_name("a.txt"))

    def test_windows_separators_are_understood(self):
        self.assertRegex(execution.unique_file_name("out" + chr(92) + "x.csv"), r"^out/x_[0-9a-f]{8}\.csv$")

    def test_personal_write_file_is_always_a_new_file_never_an_append(self):
        tok = set_personal_scope(True)
        seen = {}

        async def fake(name, args):
            seen.update(args)
            return "ok"
        try:
            with mock.patch("core.registry.registry.get", return_value=object()), \
                 mock.patch("core.registry.registry.async_run", side_effect=fake):
                run(execution.run_tool("write_file", {"path": "notes.md", "content": "x", "append": True}))
        finally:
            from core import request_context as rc
            rc._personal_scope.reset(tok)
        self.assertRegex(seen["path"], r"^notes_[0-9a-f]{8}\.md$")
        self.assertNotIn("append", seen)

    def test_agent_task_mode_is_untouched(self):
        seen = {}

        async def fake(name, args):
            seen.update(args)
            return "ok"
        with mock.patch("core.registry.registry.get", return_value=object()), \
             mock.patch("core.registry.registry.async_run", side_effect=fake):
            run(execution.run_tool("write_file", {"path": "notes.md", "content": "x"}))
        self.assertEqual(seen["path"], "notes.md")


class PersonalPreviewTests(unittest.TestCase):
    def test_preview_reads_the_agents_own_folder(self):
        from routes.agent import ws_browse
        from core import request_context as rc
        principal = mock.Mock(id=7)
        with mock.patch("core.auth_db.db_get_custom_agent", return_value={"user_id": 7, "work_dir": "D:/reports"}):
            folder, bad = ws_browse._personal_folder(3, principal)
            try:
                self.assertEqual((folder, bad), ("D:/reports", None))
                self.assertEqual(rc.personal_workspace(), "D:/reports")
            finally:
                rc._personal_workspace.set(None)

    def test_someone_elses_agent_or_no_folder_is_refused(self):
        from routes.agent import ws_browse
        principal = mock.Mock(id=7)
        for agent in ({"user_id": 8, "work_dir": "D:/theirs"}, {"user_id": 7, "work_dir": ""}, None):
            with mock.patch("core.auth_db.db_get_custom_agent", return_value=agent):
                folder, bad = ws_browse._personal_folder(3, principal)
            self.assertIsNone(folder)
            self.assertEqual(bad.status_code, 400)

    def test_no_agent_means_the_project_as_before(self):
        from routes.agent import ws_browse
        self.assertEqual(ws_browse._personal_folder(None, mock.Mock(id=7)), (None, None))

    def test_card_and_file_share_the_renamed_path(self):
        src = Path("routes/agent/run.py").read_text(encoding="utf-8")
        self.assertIn("unique_create_args(args or {})", src)
        self.assertIn("run_tool(name, args, unique_done=personal)", src)
        js = Path("static/js/preview.js").read_text(encoding="utf-8")
        self.assertIn("&agent=${encodeURIComponent(pa.id)}", js)

    def test_run_tool_does_not_rename_twice(self):
        tok = set_personal_scope(True)
        seen = {}

        async def fake(name, args):
            seen.update(args)
            return "ok"
        try:
            with mock.patch("core.registry.registry.get", return_value=object()), \
                 mock.patch("core.registry.registry.async_run", side_effect=fake):
                run(execution.run_tool("write_file", {"path": "a_1a2b3c4d.md", "content": "x"}, unique_done=True))
        finally:
            from core import request_context as rc
            rc._personal_scope.reset(tok)
        self.assertEqual(seen["path"], "a_1a2b3c4d.md")
