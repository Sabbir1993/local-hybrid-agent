"""run_shell(code=...) / cmd / script: models use other names for the command. They are resolved once,
before the permission gate and the tool, so the approval prompt and the tool see the same command."""
import asyncio
import re
import unittest
from pathlib import Path

from core.agent_loop import validate_and_repair_tool_args
from core.tool_args import SHELL_COMMAND_KEYS, shell_command

RUN_PY = Path(__file__).resolve().parent.parent / "routes" / "agent" / "run.py"


class ShellCommandNames(unittest.TestCase):
    def test_the_reported_call(self):
        self.assertEqual(shell_command({"code": "ls"}), "ls")

    def test_every_accepted_name(self):
        for key in SHELL_COMMAND_KEYS:
            self.assertEqual(shell_command({key: " dir /b "}), "dir /b", key)

    def test_command_wins_and_blank_values_are_skipped(self):
        self.assertEqual(shell_command({"command": "a", "cmd": "b"}), "a")
        self.assertEqual(shell_command({"command": "  ", "cmd": "b"}), "b")
        self.assertEqual(shell_command({"command": None, "code": "c"}), "c")

    def test_argv_list_becomes_a_command_line(self):
        self.assertEqual(shell_command({"command": ["ls", "-la"]}), "ls -la")

    def test_nothing_usable(self):
        for a in ({}, None, "ls", {"foo": "ls"}, {"command": ""}, {"command": 5}):
            self.assertEqual(shell_command(a), "")


class Repair(unittest.TestCase):
    def test_code_becomes_command_before_anything_runs(self):
        args, err = validate_and_repair_tool_args("run_shell", {"code": "ls", "timeout_s": 5})
        self.assertIsNone(err)
        self.assertEqual(args["command"], "ls")
        self.assertNotIn("code", args)                      # no stale duplicate left behind
        self.assertEqual(args["timeout_s"], 5)              # other arguments survive

    def test_missing_command_gets_an_error_that_names_the_field(self):
        args, err = validate_and_repair_tool_args("run_shell", {"foo": 1})
        self.assertIn('"command"', err)
        self.assertIn("dir", err)

    def test_other_tools_are_untouched(self):
        args, err = validate_and_repair_tool_args("run_python", {"code": "print(1)"})
        self.assertIsNone(err)
        self.assertEqual(args["code"], "print(1)")


class ToolAndGate(unittest.TestCase):
    def test_the_tool_itself_accepts_the_aliases(self):
        from core import shell_tools
        # shell disabled in a test environment: reaching that message proves the command was found
        orig = shell_tools.shell_cfg
        shell_tools.shell_cfg = lambda: {"enabled": False}
        try:
            out = asyncio.new_event_loop().run_until_complete(shell_tools.tool_run_shell({"code": "ls"}))
        finally:
            shell_tools.shell_cfg = orig
        self.assertIn("disabled", out)

    def test_the_tool_error_names_the_field(self):
        from core import shell_tools
        with self.assertRaises(ValueError) as cm:
            asyncio.new_event_loop().run_until_complete(shell_tools.tool_run_shell({}))
        self.assertIn('"command"', str(cm.exception))

    def test_the_permission_gate_reads_the_same_resolved_command(self):
        src = RUN_PY.read_text(encoding="utf-8")
        # the gate used to run only when the literal text "command" appeared in the arguments,
        # so a call under another name skipped the approval prompt
        self.assertNotIn('"command" in str(args', src)
        self.assertRegex(src, r'if name == "run_shell" and shell_command\(args\):\s+cmd = shell_command\(args\)')


if __name__ == "__main__":
    unittest.main()
