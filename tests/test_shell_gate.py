"""tests/test_shell_gate.py - shell allow-list and approval token (core/shell_tools.py).

Run: python -m unittest tests.test_shell_gate -v
"""

import asyncio
import sys
import unittest
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

    def tearDown(self):
        st.APP_CONFIG["capabilities"] = self._cfg
        st.permission_callback = self._cb

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


if __name__ == "__main__":
    unittest.main()
