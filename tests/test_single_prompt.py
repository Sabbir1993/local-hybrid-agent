"""tests/test_single_prompt.py - the web app's permission card is the single approval for
run_python / run_shell: the companion is told `approved_in_app` only for exactly the code or
command that passed the server gate. Also: AgentRequest accepts the answer-check `verify`.

Run: python -m unittest tests.test_single_prompt
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import agent_tools, shell_tools


class _Companion:
    def __init__(self):
        self.calls = []

    async def call(self, uid, op, params, timeout=60):
        self.calls.append((op, params))
        return {"exit_code": 0, "stdout": "ok"}


class SinglePromptTests(unittest.TestCase):
    def setUp(self):
        self.comp = _Companion()
        self._p = [
            mock.patch.object(agent_tools.companion_bridge, "call", self.comp.call),
            mock.patch.object(agent_tools, "require_device_workspace", return_value=(7, Path("C:/proj"))),
            mock.patch.object(shell_tools, "shell_cfg", return_value={"enabled": True, "ask_first": True}),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def _run_python(self, code):
        asyncio.run(agent_tools.tool_run_python({"code": code}))
        return [c[1] for c in self.comp.calls if c[0] == "shell.run"][-1]

    def test_approved_code_skips_companion_dialog_once(self):
        async def go():
            shell_tools.mark_code_approved("print(1)")
            await agent_tools.tool_run_python({"code": "print(1)"})
            await agent_tools.tool_run_python({"code": "print(1)"})   # approval is single use
        asyncio.run(go())
        runs = [c[1] for c in self.comp.calls if c[0] == "shell.run"]
        self.assertTrue(runs[0]["approved_in_app"])
        self.assertFalse(runs[1]["approved_in_app"])

    def test_other_code_is_not_approved(self):
        async def go():
            shell_tools.mark_code_approved("print(1)")
            await agent_tools.tool_run_python({"code": "import os; os.remove('x')"})
        asyncio.run(go())
        self.assertFalse(self.comp.calls[-1][1]["approved_in_app"])

    def test_ask_first_off_counts_as_approved(self):
        with mock.patch.object(shell_tools, "shell_cfg", return_value={"enabled": True, "ask_first": False}):
            self.assertTrue(self._run_python("print(2)")["approved_in_app"])


class AgentRequestTests(unittest.TestCase):
    def test_verify_field(self):
        from routes.agent import AgentRequest
        self.assertIsNone(AgentRequest(messages=[]).verify)
        self.assertEqual(AgentRequest(messages=[], verify="badge").verify, "badge")


if __name__ == "__main__":
    unittest.main()
