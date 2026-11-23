"""P1 wiring: the isolated runner's tool is callable from the agent loop but is
only *offered* when the runner is healthy, and always fails closed otherwise.

No runner or network is contacted in these tests.
"""

import asyncio
import pathlib
import unittest
from unittest.mock import AsyncMock, patch

from core import request_context as rc
from core import tool_surface
from core.agent_loop import run_tool
from core.agent_tools import AGENT_TOOLS, TOOL_IMPLS
from core.subagent.constants import DENIED_TOOLS


class RegistrationTests(unittest.TestCase):
    def test_impl_registered_but_schema_not_offered_globally(self):
        self.assertIn("run_sandbox_python", TOOL_IMPLS)
        names = {(t.get("function") or {}).get("name") for t in AGENT_TOOLS}
        self.assertNotIn("run_sandbox_python", names)

    def test_subagents_still_denied_all_code_tools(self):
        for name in ("run_python", "run_shell", "run_sandbox_python"):
            self.assertIn(name, DENIED_TOOLS)


class FailClosedTests(unittest.TestCase):
    def test_no_authenticated_user_refuses(self):
        rc.set_current_user(None)
        try:
            out = asyncio.run(run_tool("run_sandbox_python", {"code": "print(1)"}))
        finally:
            rc.set_current_user(None)
        self.assertTrue(str(out).startswith("error:"))
        self.assertIn("authenticated", str(out).lower())

    def test_unhealthy_runner_refuses(self):
        rc.set_current_user(1)
        with patch("core.sandbox.admin.ready", new=AsyncMock(return_value=False)):
            out = asyncio.run(run_tool("run_sandbox_python", {"code": "print(1)"}))
        self.assertTrue(str(out).startswith("error:"))
        self.assertIn("unavailable", str(out).lower())

    def test_healthy_runner_forwards_code_and_uid(self):
        rc.set_current_user(42)
        with patch("core.sandbox.admin.ready", new=AsyncMock(return_value=True)), \
             patch("core.sandbox.tool_run_sandbox_python", new=AsyncMock(return_value="ok")) as mocked:
            out = asyncio.run(run_tool("run_sandbox_python", {"code": "print(1)"}))
        self.assertEqual(out, "ok")
        mocked.assert_awaited_once_with({"code": "print(1)"}, 42)


class SurfaceTests(unittest.TestCase):
    def test_family_hidden_unless_request_needs_code(self):
        schema = {"function": {"name": "run_sandbox_python"}}
        self.assertNotIn(schema, tool_surface.filter_tools([schema], "fix the README typo"))
        self.assertIn(schema, tool_surface.filter_tools([schema], "write a python script to clean the data"))
        self.assertIn("sandbox", tool_surface.hidden_families("fix the README typo"))


class WiringTests(unittest.TestCase):
    """Source-level guards: the schema is offered only behind the health check,
    and plan mode stays read-only."""

    ROOT = pathlib.Path(__file__).resolve().parents[1]

    def test_agent_offers_schema_only_when_runner_answers(self):
        src = (self.ROOT / "routes" / "agent" / "stream.py").read_text(encoding="utf-8")
        self.assertIn("if await _agent_sandbox.available():", src)
        self.assertIn("sandbox_tools = [_agent_sandbox.RUN_SANDBOX_SCHEMA]", src)
        self.assertIn("all_tools() + sandbox_tools", src)

    def test_plan_mode_never_offers_code_tools(self):
        from routes.agent.constants import PLAN_MODE_TOOLS
        for name in ("run_sandbox_python", "run_python", "run_shell"):
            self.assertNotIn(name, PLAN_MODE_TOOLS)


if __name__ == "__main__":
    unittest.main()
