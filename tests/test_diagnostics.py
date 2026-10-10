"""tests/test_diagnostics.py - lint feedback after edits (fs.diagnose through the verify loop).

Run: python -m unittest tests.test_diagnostics -v
"""

import asyncio
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import agent_tools, companion_bridge
from core.agent_tools import file_state, verify_loop
from core.small_model import APP_CONFIG
from core.request_context import set_current_device, set_current_user

WS = r"C:\Users\[PLACEHOLDER]\projects\demo"


def full(name):
    return str(Path(WS) / name)


class Device:
    def __init__(self):
        self.files = {}
        self.diag = {"checked": True, "tool": "ruff", "issues": [], "total": 0}
        self.diag_calls = []
        self.diag_error = None

    async def call(self, uid, op, params, timeout=None):
        if op == "fs.read":
            return {"content": self.files.get(params["path"])}
        if op == "fs.write":
            self.files[params["path"]] = params["content"]
            return {"existed": True}
        if op == "fs.verify":
            return {"checked": False}
        if op == "fs.diagnose":
            self.diag_calls.append(params)
            if self.diag_error:
                raise self.diag_error
            return self.diag
        raise RuntimeError(f"unknown op {op}")


class Base(unittest.TestCase):
    def setUp(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE projects (name TEXT, user_id INTEGER, device_id TEXT, workspace_dir TEXT)")
        db.execute("INSERT INTO projects VALUES ('demo', 7, 'dev1', ?)", (WS,))
        self.dev = Device()
        self.dev.files[full("m.py")] = "def f():\n    return 1\n"
        self.dev.files[full("t.ts")] = "export const a = 1;\n"
        self._p = [
            mock.patch.object(agent_tools, "_projects_db", db),
            mock.patch.object(companion_bridge, "is_connected", lambda uid: uid == 7),
            mock.patch.object(companion_bridge, "call", self.dev.call),
            mock.patch.dict(agent_tools._active_project, {"7:dev1": "demo"}, clear=True),
            mock.patch.dict(agent_tools._ws_changes, {}, clear=True),
            mock.patch.dict(agent_tools._file_diffs, {}, clear=True),
            mock.patch.dict(file_state._read_sets, {}, clear=True),
            mock.patch.dict(file_state._undo, {}, clear=True),
            mock.patch.dict(file_state._verify, {}, clear=True),
        ]
        for p in self._p:
            p.start()
        set_current_user(7)
        set_current_device("dev1")

    def tearDown(self):
        for p in self._p:
            p.stop()
        set_current_user(None)
        set_current_device(None)

    def call(self, tool, **args):
        return asyncio.run(agent_tools.TOOL_IMPLS[tool](args))

    def edit(self, path="m.py", old="return 1", new="return 2"):
        self.call("read_file", path=path)
        return self.call("edit_file", path=path, old_string=old, new_string=new)


class LintAfterEdit(Base):
    def test_clean_edit_stays_silent(self):
        out = self.edit()
        self.assertIn("verify: OK", out)
        self.assertNotIn("lint", out)
        self.assertEqual(len(self.dev.diag_calls), 1)
        self.assertEqual(self.dev.diag_calls[0]["path"], full("m.py"))
        self.assertIn("root", self.dev.diag_calls[0])

    def test_findings_are_reported_with_line_code_and_message(self):
        self.dev.diag = {"checked": True, "tool": "ruff", "total": 2, "issues": [
            {"line": 2, "col": 12, "code": "F821", "message": "Undefined name `total`"},
            {"line": 9, "col": 4, "code": "F632", "message": "Use `==` to compare constant literals"}]}
        out = self.edit()
        self.assertIn("verify: OK", out, "lint is advisory: the edit still counts as verified")
        self.assertIn("lint (ruff): 2 problems in m.py", out)
        self.assertIn("L2 F821 Undefined name `total`", out)
        self.assertIn("L9 F632", out)
        self.assertIn("Fix these before you finish", out)

    def test_many_findings_are_capped(self):
        self.dev.diag = {"checked": True, "tool": "eslint", "total": 12, "issues": [
            {"line": i, "col": 1, "code": "no-undef", "message": f"'v{i}' is not defined."} for i in range(1, 13)]}
        out = self.edit()
        self.assertEqual(out.count("no-undef"), verify_loop.MAX_LINT_SHOWN)
        self.assertIn("+7 more", out)

    def test_lint_never_triggers_the_auto_restore(self):
        self.dev.diag = {"checked": True, "tool": "ruff", "total": 1,
                         "issues": [{"line": 1, "col": 1, "code": "F821", "message": "x"}]}
        for _ in range(8):                           # well past verify_max_retries
            out = self.edit(old="return 1" if _ % 2 == 0 else "return 2", new="return 2" if _ % 2 == 0 else "return 1")
            self.assertNotIn("restored", out)
        self.assertIn("lint (ruff)", out)

    def test_typescript_which_has_no_syntax_check_still_gets_lint(self):
        self.dev.diag = {"checked": True, "tool": "eslint", "total": 1,
                         "issues": [{"line": 1, "col": 1, "code": "no-undef", "message": "'b' is not defined."}]}
        out = self.edit(path="t.ts", old="= 1", new="= b")
        self.assertIn("lint (eslint): 1 problem in t.ts", out)
        self.assertNotIn("verify:", out)


class WhenLintDoesNotRun(Base):
    def test_skeleton_writes_and_appends_are_not_linted(self):
        self.call("write_file", path="new.py", content="def f():\n    pass\n")
        self.call("append_file", path="new.py", content="\ndef g():\n    pass\n")
        self.assertEqual(self.dev.diag_calls, [], "a half-written file would only produce noise")

    def test_a_syntax_failure_skips_lint(self):
        out = self.edit(old="return 1", new="return (")
        self.assertIn("verify: FAILED", out)
        self.assertEqual(self.dev.diag_calls, [])

    def test_an_old_companion_without_the_op_is_silent(self):
        self.dev.diag_error = RuntimeError("unknown op fs.diagnose")
        out = self.edit()
        self.assertIn("verify: OK", out)
        self.assertNotIn("lint", out)

    def test_unchecked_files_are_silent(self):
        self.dev.diag = {"checked": False, "reason": "ruff not installed"}
        self.assertNotIn("lint", self.edit())

    def test_it_can_be_switched_off(self):
        with mock.patch.dict(APP_CONFIG, {"agent": {**(APP_CONFIG.get("agent") or {}), "diagnostics": False}}):
            self.edit()
        self.assertEqual(self.dev.diag_calls, [])


class FailingFiles(Base):
    """The plan's 'done' gate asks which files the verify loop last found broken."""

    def test_tracks_the_last_syntax_result_per_file(self):
        self.assertEqual(file_state.failing_files(), [])
        self.assertIn("verify: FAILED", self.edit(old="return 1", new="return ("))
        self.assertEqual(file_state.failing_files(), [full("m.py")])
        self.assertIn("verify: OK", self.edit(old="return (", new="return 3"))
        self.assertEqual(file_state.failing_files(), [], "a passing write clears it")


class Wiring(unittest.TestCase):
    def test_the_op_is_read_only_so_it_is_safe_to_retry(self):
        from core.companion_bridge.constants import READ_ONLY_OPS
        self.assertIn("fs.diagnose", READ_ONLY_OPS)


if __name__ == "__main__":
    unittest.main()
