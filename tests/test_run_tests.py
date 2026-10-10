"""tests/test_run_tests.py - runner detection, command building, output summaries and the approval contract.

Run: python -m unittest tests.test_run_tests -v
"""

import asyncio
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import agent_tools, companion_bridge, shell_tools
from core.agent_tools import test_runner as tr
from core.request_context import set_current_device, set_current_user

WS = r"C:\Users\[PLACEHOLDER]\projects\demo"

PYTEST_FAIL = """\
.F.F                                                                     [100%]
=================================== FAILURES ===================================
__________________________________ test_login __________________________________

    def test_login():
>       assert login("a") == 2
E       assert 1 == 2
E        +  where 1 = login('a')

tests/test_auth.py:12: AssertionError
______________________________ TestApi.test_token ______________________________

    def test_token(self):
>       raise ValueError("bad token")
E       ValueError: bad token

tests/test_auth.py:30: ValueError
=========================== short test summary info ============================
FAILED tests/test_auth.py::test_login - assert 1 == 2
FAILED tests/test_auth.py::TestApi::test_token - ValueError: bad token
2 failed, 2 passed in 0.31s
"""
JEST_FAIL = """\
 FAIL  src/add.test.js
  ● adds numbers

    expect(received).toBe(expected)

      at Object.<anonymous> (src/add.test.js:3:19)

Tests:       1 failed, 4 passed, 5 total
"""
GO_FAIL = """\
--- FAIL: TestAdd (0.00s)
    add_test.go:9: got 3, want 4
FAIL
FAIL\texample.com/m\t0.002s
"""
CARGO_FAIL = """\
running 2 tests
test tests::ok ... ok
test tests::bad ... FAILED
test result: FAILED. 1 passed; 1 failed; 0 ignored
"""


def shell_result(code, out="", err=""):
    r = f"exit code {code}"
    if out:
        r += f"\n--- stdout ---\n{out}"
    if err:
        r += f"\n--- stderr ---\n{err}"
    return r


class Detect(unittest.TestCase):
    def test_pytest_by_config_or_conftest(self):
        self.assertEqual(tr.detect({"conftest.py"}, {})["runner"], "pytest")
        self.assertEqual(tr.detect({"pyproject.toml"}, {"pyproject.toml": "[tool.pytest.ini_options]\n"})["runner"], "pytest")
        self.assertEqual(tr.detect({"requirements.txt"}, {"requirements.txt": "flask\npytest>=8\n"})["runner"], "pytest")

    def test_unittest_when_tests_exist_and_pytest_is_not_mentioned(self):
        f = tr.detect({"tests/test_a.py", "app.py"}, {})
        self.assertEqual(f["runner"], "unittest")
        self.assertEqual(f["base"], "python -m unittest discover -s tests")
        self.assertEqual(tr.detect({"test_a.py"}, {})["base"], "python -m unittest discover")

    def test_npm_test_script_and_the_default_placeholder(self):
        pkg = json.dumps({"scripts": {"test": "jest"}})
        self.assertEqual(tr.detect({"package.json"}, {"package.json": pkg})["runner"], "npm")
        stub = json.dumps({"scripts": {"test": 'echo "Error: no test specified" && exit 1'}})
        self.assertIsNone(tr.detect({"package.json"}, {"package.json": stub}))

    def test_bare_vitest_would_watch_so_it_runs_once(self):
        pkg = json.dumps({"scripts": {"test": "vitest"}})
        f = tr.detect({"package.json"}, {"package.json": pkg})
        self.assertEqual((f["runner"], f["base"]), ("vitest", "npx --no-install vitest run"))
        run = json.dumps({"scripts": {"test": "vitest run"}})
        self.assertEqual(tr.detect({"package.json"}, {"package.json": run})["runner"], "npm")

    def test_go_cargo_and_nothing(self):
        self.assertEqual(tr.detect({"go.mod"}, {})["runner"], "go")
        self.assertEqual(tr.detect({"Cargo.toml"}, {})["runner"], "cargo")
        self.assertIsNone(tr.detect({"README.md"}, {}))
        self.assertIsNone(tr.detect({"package.json"}, {"package.json": "{not json"}))


class BuildCommand(unittest.TestCase):
    def test_commands(self):
        py = tr.detect({"conftest.py"}, {})
        self.assertEqual(tr.build_command(py), py["base"])
        self.assertEqual(tr.build_command(py, "tests/test_a.py::test_x"), py["base"] + " tests/test_a.py::test_x")
        ut = tr.detect({"tests/test_a.py"}, {})
        self.assertEqual(tr.build_command(ut, "tests/test_a.py"), "python -m unittest tests.test_a")
        self.assertEqual(tr.build_command(ut, "tests/sub"), "python -m unittest discover -s tests/sub")
        npm = {"runner": "npm", "base": "npm test --silent"}
        self.assertEqual(tr.build_command(npm, "src/a.test.js"), "npm test --silent -- src/a.test.js")
        go = {"runner": "go", "base": "go test"}
        self.assertEqual(tr.build_command(go), "go test ./...")
        self.assertEqual(tr.build_command(go, "pkg/x"), "go test ./pkg/x/...")
        self.assertEqual(tr.build_command(go, "pkg/x/a_test.go"), "go test ./pkg/x")

    def test_unsafe_paths_are_refused(self):
        for bad in ("a b", "x;rm -rf .", "$(id)", "`id`", "-k", "--rootdir=/", "../x", "/etc/passwd", "C:/x",
                    'a"b', "a&b", "a|b", "a>b"):
            with self.subTest(path=bad):
                with self.assertRaises(tr.NoRunner):
                    tr._safe_path(bad)
        self.assertEqual(tr._safe_path("tests\\test_a.py::T::test[a-1]"), "tests/test_a.py::T::test[a-1]")


class Summaries(unittest.TestCase):
    def test_pytest_failures_have_ids_messages_and_locations(self):
        s = tr.summarize("pytest", "cmd", shell_result(1, PYTEST_FAIL))
        self.assertIn("FAILED (exit 1): 2 failed, 2 passed in 0.31s", s)
        self.assertIn("tests/test_auth.py::test_login - assert 1 == 2 (tests/test_auth.py:12)", s)
        self.assertIn("TestApi::test_token - ValueError: bad token (tests/test_auth.py:30)", s)
        self.assertNotIn("where 1 = login", s, "tracebacks are not dumped")

    def test_all_passed_and_nothing_collected(self):
        ok = tr.summarize("pytest", "cmd", shell_result(0, "....\n4 passed in 0.10s\n"))
        self.assertIn("all passed: 4 passed in 0.10s", ok)
        self.assertIn("no tests were collected", tr.summarize("pytest", "cmd", shell_result(5, "no tests ran in 0.01s")))

    def test_missing_dependency_is_not_reported_as_a_test_failure(self):
        s = tr.summarize("pytest", "cmd", shell_result(2, "", "ModuleNotFoundError: No module named 'flask'"))
        self.assertIn("missing module 'flask'", s)
        self.assertIn("not a test failure", s)
        self.assertNotIn("Failures:", s)

    def test_command_not_found(self):
        s = tr.summarize("npm", "cmd", shell_result(1, "", "'npm' is not recognized as an internal or external command"))
        self.assertIn("not installed or not on PATH", s)

    def test_failure_list_is_capped(self):
        out = "\n".join(f"FAILED tests/t.py::test_{i} - boom" for i in range(20)) + "\n20 failed in 1.00s"
        s = tr.summarize("pytest", "cmd", shell_result(1, out))
        self.assertEqual(s.count(" tests/t.py::test_"), tr.MAX_FAILURES)
        self.assertIn("12 more", s)

    def test_other_runners(self):
        js = tr.summarize("npm", "cmd", shell_result(1, JEST_FAIL))
        self.assertIn("Tests: 1 failed, 4 passed, 5 total", js)
        self.assertIn("● adds numbers", js)
        go = tr.summarize("go", "cmd", shell_result(1, GO_FAIL))
        self.assertIn("--- FAIL: TestAdd", go)
        cargo = tr.summarize("cargo", "cmd", shell_result(101, CARGO_FAIL))
        self.assertIn("test tests::bad ... FAILED", cargo)

    def test_unknown_output_falls_back_to_the_tail(self):
        s = tr.summarize("npm", "cmd", shell_result(1, "\n".join(f"line {i}" for i in range(100))))
        self.assertIn("No structured failures found", s)
        self.assertIn("line 99", s)
        self.assertNotIn("line 10\n", s)

    def test_shell_errors_pass_through(self):
        self.assertEqual(tr.summarize("pytest", "cmd", "error: user denied shell command: cmd"),
                         "error: user denied shell command: cmd")

    def test_a_real_unittest_run_is_parsed(self):
        """Genuine output from `python -m unittest`, not a hand-written sample."""
        with tempfile.TemporaryDirectory() as td:
            t = Path(td) / "tests"
            t.mkdir()
            (t / "__init__.py").write_text("", encoding="utf-8")
            (t / "test_calc.py").write_text(
                "import unittest\n\nclass T(unittest.TestCase):\n"
                "    def test_ok(self):\n        self.assertEqual(1, 1)\n"
                "    def test_bad(self):\n        self.assertEqual(1, 2)\n"
                "    def test_boom(self):\n        raise KeyError('x')\n", encoding="utf-8")
            r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], cwd=td,
                               capture_output=True, text=True)
        s = tr.summarize("unittest", "cmd", shell_result(r.returncode, r.stdout, r.stderr))
        self.assertIn("Ran 3 tests", s)
        self.assertIn("FAILED (failures=1, errors=1)", s)
        self.assertIn("FAIL test_bad (test_calc.T.test_bad)", s)
        self.assertIn("AssertionError: 1 != 2", s)
        self.assertIn("ERROR test_boom", s)
        self.assertIn("KeyError: 'x'", s)
        self.assertIn("test_calc.py:", s)


class FakeDevice:
    def __init__(self, files, shell=None):
        self.files = files            # rel -> text
        self.shell_calls = []
        self.shell = shell or (lambda cmd: shell_result(0, "ok"))

    async def call(self, uid, op, params, timeout=None):
        if op == "fs.list":
            pat = params["pattern"]
            if pat == "*":
                return {"files": [f for f in self.files if "/" not in f]}
            if pat.endswith("/*"):
                d = pat[:-1]
                return {"files": [f for f in self.files if f.startswith(d)]}
            return {"files": []}
        if op == "fs.read":
            rel = params["path"].replace("\\", "/").rsplit("/", 1)[-1]
            return {"content": self.files.get(rel)}
        if op == "shell.run":
            self.shell_calls.append(params)
            res = self.shell(params["command"])
            code, out, err = tr.split_result(res)
            return {"exit_code": code, "stdout": out, "stderr": err}
        raise RuntimeError(f"unknown op {op}")


class ToolTests(unittest.TestCase):
    cfg = {"enabled": True, "ask_first": False, "allow_patterns": [], "timeout_s": 60}

    def setUp(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE projects (name TEXT, user_id INTEGER, device_id TEXT, workspace_dir TEXT)")
        db.execute("INSERT INTO projects VALUES ('demo', 7, 'dev1', ?)", (WS,))
        self.dev = FakeDevice({"conftest.py": "", "app.py": "x = 1"})
        tr.clear_cache()
        self._p = [
            mock.patch.object(agent_tools, "_projects_db", db),
            mock.patch.object(companion_bridge, "is_connected", lambda uid: uid == 7),
            mock.patch.object(companion_bridge, "call", self.dev.call),
            mock.patch.dict(agent_tools._active_project, {"7:dev1": "demo"}, clear=True),
            mock.patch.object(shell_tools, "shell_cfg", lambda: dict(self.cfg)),
        ]
        for p in self._p:
            p.start()
        set_current_user(7)
        set_current_device("dev1")

    def tearDown(self):
        for p in self._p:
            p.stop()
        tr.clear_cache()
        set_current_user(None)
        set_current_device(None)

    def run_(self, **args):
        return asyncio.run(tr.tool_run_tests(args))

    def test_runs_the_detected_command_with_a_test_sized_timeout(self):
        self.dev.shell = lambda cmd: shell_result(1, PYTEST_FAIL)
        out = self.run_()
        self.assertIn("2 failed, 2 passed", out)
        call = self.dev.shell_calls[0]
        self.assertEqual(call["command"], "python -m pytest -q --tb=short -rf --maxfail=15")
        self.assertEqual(call["timeout"], tr.DEFAULT_TIMEOUT_S, "a suite gets more than the 60 s shell default")

    def test_path_is_appended_and_validated(self):
        self.run_(path="tests/test_auth.py::test_login")
        self.assertTrue(self.dev.shell_calls[0]["command"].endswith(" tests/test_auth.py::test_login"))
        self.dev.shell_calls.clear()
        self.assertIn("path must be", self.run_(path="x; rm -rf /"))
        self.assertEqual(self.dev.shell_calls, [], "nothing ran")

    def test_no_runner_is_explained(self):
        self.dev.files = {"README.md": "hi"}
        tr.clear_cache()
        self.assertIn("no test runner detected", self.run_())
        self.assertEqual(self.dev.shell_calls, [])

    def test_the_model_cannot_raise_the_timeout(self):
        self.cfg = dict(self.cfg, tests_timeout_s=99999)
        self.run_()
        self.assertEqual(self.dev.shell_calls[0]["timeout"], tr.MAX_TIMEOUT_S)
        # a plain run_shell call afterwards is back on the admin's limit
        asyncio.run(shell_tools.tool_run_shell({"command": "dir"}))
        self.assertEqual(self.dev.shell_calls[-1]["timeout"], 60)

    def test_it_goes_through_the_shell_approval_gate(self):
        self.cfg = dict(self.cfg, ask_first=True)
        out = self.run_()
        self.assertIn("user denied shell command", out, "unapproved, so refused")
        self.assertEqual(self.dev.shell_calls, [])

        async def approved():
            cmd = await tr.command_for({})
            shell_tools.mark_approved(cmd)           # what the loop does after the user says yes
            return await tr.tool_run_tests({})
        self.assertIn("all passed", asyncio.run(approved()))
        self.assertEqual(len(self.dev.shell_calls), 1)

    def test_command_for_matches_what_the_tool_runs(self):
        cmd = asyncio.run(tr.command_for({"path": "tests/test_a.py"}))
        self.run_(path="tests/test_a.py")
        self.assertEqual(cmd, self.dev.shell_calls[0]["command"])
        self.assertIsNone(asyncio.run(tr.command_for({"path": "bad path"})))

    def test_detection_is_cached_between_the_loop_and_the_tool(self):
        calls = []
        inner = self.dev.call

        async def counting(uid, op, params, timeout=None):
            calls.append(op)
            return await inner(uid, op, params, timeout)
        with mock.patch.object(companion_bridge, "call", counting):
            asyncio.run(tr.command_for({}))
            n = len([c for c in calls if c != "shell.run"])
            asyncio.run(tr.tool_run_tests({}))
        self.assertEqual(len([c for c in calls if c != "shell.run"]), n, "no second round of fs.list / fs.read")


class Wiring(unittest.TestCase):
    def test_registered_and_fenced_off_where_code_must_not_run(self):
        from core.request_context import PERSONAL_BLOCKED_TOOLS
        from core.subagent.constants import DENIED_TOOLS
        from routes.agent import constants
        shell_tools.register_shell_tools()
        from core.registry import registry
        self.assertIsNotNone(registry.get("run_tests"))
        self.assertIn("run_tests", DENIED_TOOLS)
        self.assertIn("run_tests", PERSONAL_BLOCKED_TOOLS)
        self.assertNotIn("run_tests", constants.PLAN_MODE_TOOLS, "it executes code, so not in read-only plan mode")
        self.assertNotIn("run_tests", constants.PARALLEL_READ_TOOLS)


if __name__ == "__main__":
    unittest.main()
