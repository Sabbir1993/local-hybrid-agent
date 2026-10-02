"""tests/test_shell_gate.py - shell allow-list and approval token (core/shell_tools.py).

Run: python -m unittest tests.test_shell_gate -v
"""

import asyncio
import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import shell_tools as st


class AllowListTests(unittest.TestCase):
    PATS = ["git *", "npm *"]

    def test_simple_command_matches(self):
        self.assertTrue(st.command_allowed("git status", self.PATS))
        self.assertTrue(st.command_allowed("  NPM install  ", self.PATS))

    def test_chained_commands_never_match_a_prefix_pattern(self):
        for cmd in ("git status & del /q *.*", "git log | findstr x", "git status && calc",
                    "git log > evil.bat", "git status ; rm -rf x", "git status\ncalc",
                    "git log `whoami`", "git log $(whoami)", "git log %USERPROFILE%",
                    "git status ^& calc"):
            self.assertFalse(st.command_allowed(cmd, self.PATS), cmd)

    def test_star_pattern_allows_everything(self):
        self.assertTrue(st.command_allowed("git status & dir", ["*"]))

    def test_unlisted(self):
        self.assertFalse(st.command_allowed("curl http://x", self.PATS))
        self.assertFalse(st.command_allowed("", self.PATS))


class ApprovalTokenTests(unittest.TestCase):
    def setUp(self):
        self._cfg = st.APP_CONFIG.get("capabilities")
        st.APP_CONFIG["capabilities"] = {"shell": {"enabled": True, "ask_first": True,
                                                   "allow_patterns": []}}
        self._cb = st.permission_callback
        st.permission_callback = None      # no modal channel -> deny unless approved

        # approved commands run on the user's machine via the companion -- never the server
        async def fake_call(uid, op, params, timeout=None):
            assert op == "shell.run"
            return {"exit_code": 0, "stdout": params["command"].replace("echo ", "") + "\n"}
        self._patches = [
            mock.patch("core.agent_tools.require_device_workspace",
                       lambda: (7, Path(r"C:\Users\[PLACEHOLDER]\proj"))),
            mock.patch.object(st.companion_bridge, "call", fake_call),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        st.APP_CONFIG["capabilities"] = self._cfg
        st.permission_callback = self._cb
        for p in self._patches:
            p.stop()

    def test_model_supplied_flag_is_ignored(self):
        out = asyncio.run(st.tool_run_shell({"command": "whoami", "_pre_approved": True}))
        self.assertIn("denied", out)

    def test_token_only_covers_the_approved_command(self):
        async def main():
            st.mark_approved("echo approved")
            return await st.tool_run_shell({"command": "whoami"})
        self.assertIn("denied", asyncio.run(main()))

    def test_token_is_single_use_and_runs_approved_command(self):
        async def main():
            st.mark_approved("echo approved-ok")
            first = await st.tool_run_shell({"command": "echo approved-ok"})
            second = await st.tool_run_shell({"command": "echo approved-ok"})
            return first, second
        first, second = asyncio.run(main())
        self.assertIn("approved-ok", first)
        self.assertIn("denied", second)


class MissingEnvIsDiagnosableTests(unittest.TestCase):
    """A command broken by the companion's filtered env must be told WHY.

    companion/shellops.js now hands agent-run commands a filtered environment
    (secrets removed). Windows reports a missing binary as
    "'X' is not recognized as an internal or external command" on stderr with no
    exit-code clue, so without this the agent sees an opaque failure and retries
    the same broken command forever.
    """

    def setUp(self):
        self._cfg = st.APP_CONFIG.get("capabilities")
        st.APP_CONFIG["capabilities"] = {"shell": {"enabled": True, "ask_first": False,
                                                   "allow_patterns": ["*"]}}
        self._cb = st.permission_callback
        st.permission_callback = None

        async def fake_call(uid, op, params, timeout=None):
            return {"exit_code": 1, "stdout": "",
                    "stderr": "'npm' is not recognized as an internal or external command,\n"
                              "operable program or batch file.\n"}
        self._patches = [
            mock.patch("core.agent_tools.require_device_workspace",
                       lambda: (7, Path(r"C:\Users\dev\proj"))),
            mock.patch.object(st.companion_bridge, "call", fake_call),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        st.APP_CONFIG["capabilities"] = self._cfg
        st.permission_callback = self._cb
        for p in self._patches:
            p.stop()

    def test_env_cause_is_explained(self):
        out = asyncio.run(st.tool_run_shell({"command": "npm test"}))
        self.assertIn("not recognized", out, "the raw OS message must still be shown")
        self.assertIn("COMPANION_ENV_ALLOW", out,
                      "the fix must be named, or the model retries the same call forever")

    def test_a_normal_failure_is_not_blamed_on_the_environment(self):
        async def failing(uid, op, params, timeout=None):
            return {"exit_code": 1, "stdout": "", "stderr": "2 tests failed\n"}
        with mock.patch.object(st.companion_bridge, "call", failing):
            out = asyncio.run(st.tool_run_shell({"command": "npm test"}))
        self.assertIn("2 tests failed", out)
        self.assertNotIn("COMPANION_ENV_ALLOW", out,
                         "an ordinary test failure must not be misdiagnosed as an env problem")


if __name__ == "__main__":
    unittest.main()
