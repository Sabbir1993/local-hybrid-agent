"""tests/test_mcp_spawn.py - MCP client: Windows command resolution, stderr drain, disabled/merged config, PAN masking.

Run: python -m unittest tests.test_mcp_spawn -v
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import mcp
from core.small_model import APP_CONFIG

ECHO_SERVER = str(Path(__file__).resolve().parents[1] / "core" / "echo_mcp_server.py")
VISA_TEST = "4111 1111 1111 1111"   # public test PAN, not a real card


class SpawnTests(unittest.TestCase):
    def test_command_resolved_via_which(self):
        srv = mcp.McpServer("x", {"command": "npx", "args": ["-y", "mcp-remote", "https://example.invalid/mcp"]})
        with mock.patch.object(mcp.shutil, "which", return_value=r"C:\node\npx.cmd") as w, \
             mock.patch.object(mcp.subprocess, "Popen") as popen, \
             mock.patch.object(mcp.threading, "Thread"):
            srv._spawn_stdio()
        w.assert_called_once_with("npx")
        self.assertEqual(popen.call_args[0][0][0], r"C:\node\npx.cmd")

    def test_missing_command_reports_clearly(self):
        srv = mcp.McpServer("x", {"command": "definitely-not-a-command"})
        with mock.patch.object(mcp.shutil, "which", return_value=None):
            with self.assertRaises(RuntimeError) as cm:
                srv._spawn_stdio()
        self.assertIn("not found on PATH", str(cm.exception))

    def test_npx_gets_long_init_timeout(self):
        self.assertEqual(mcp.McpServer("a", {"command": "npx"})._init_timeout(), mcp.NPX_INIT_TIMEOUT_S)
        self.assertEqual(mcp.McpServer("b", {"command": "python"})._init_timeout(), mcp.INIT_TIMEOUT_S)
        self.assertEqual(mcp.McpServer("c", {"command": "npx", "init_timeout": 5})._init_timeout(), 5.0)

    def test_echo_server_connects_end_to_end(self):
        srv = mcp.McpServer("echo_t", {"command": sys.executable, "args": [ECHO_SERVER]})
        try:
            tools = asyncio.run(srv.connect())
        finally:
            srv.stop()
        self.assertEqual(srv.status, "stopped")
        self.assertTrue(tools, srv.error)

    def test_failure_includes_stderr_tail(self):
        code = "import sys; sys.stderr.write('boom: auth required\\n'); sys.stderr.flush()"
        srv = mcp.McpServer("bad", {"command": sys.executable, "args": ["-c", code], "init_timeout": 5})
        asyncio.run(srv.connect())
        self.assertEqual(srv.status, "error")
        self.assertIn("boom: auth required", srv.error)


class ConfigTests(unittest.TestCase):
    def test_mcpservers_block_merged_and_explicit_wins(self):
        cfg = {"mcpServers": {"a": {"command": "npx"}, "b": {"command": "node"}},
               "capabilities": {"mcp_servers": {"b": {"command": "python"}}}}
        merged = mcp.configured_servers(cfg)
        self.assertEqual(set(merged), {"a", "b"})
        self.assertEqual(merged["b"]["command"], "python")

    def test_disabled_server_not_started(self):
        caps = APP_CONFIG.setdefault("capabilities", {})
        with mock.patch.dict(caps, {"mcp": True, "mcp_servers": {"off": {"command": "npx", "disabled": True}}}), \
             mock.patch.dict(APP_CONFIG, {"mcpServers": {}}), \
             mock.patch("core.auth_db.list_user_mcp_servers", return_value=[]), \
             mock.patch.object(mcp.McpServer, "connect") as conn:
            res = asyncio.run(mcp.connect_all_mcp())
        conn.assert_not_called()
        self.assertEqual(res["off"]["status"], "disabled")
        mcp._servers.pop("off", None)


class ChatExposureTests(unittest.TestCase):
    """Chat/agent must see connected MCP tools and be told what each server is."""

    def setUp(self):
        self.srv = mcp.McpServer("isms_t", {"command": "npx"})
        self.srv.status = "ready"
        self.srv.tools = [{"name": "list_endpoints",
                           "description": "List all available SSL Wireless ISMSPLUS API v3 endpoints."}]
        mcp._servers["isms_t"] = self.srv
        mcp.registry.register("mcp__isms_t__list_endpoints", mcp._tool_bridge(self.srv, "list_endpoints"),
                              mcp._bridge_schema("isms_t", self.srv.tools[0]), source="mcp:isms_t",
                              meta={}, replace=True)

    def tearDown(self):
        mcp.disconnect_one("isms_t")

    def test_ready_server_tools_and_prompt_exposed(self):
        names = [s["function"]["name"] for s in mcp.ready_tool_schemas()]
        self.assertIn("mcp__isms_t__list_endpoints", names)
        prompt = mcp.chat_prompt()
        self.assertIn("`isms_t`", prompt)
        self.assertIn("ISMSPLUS", prompt)
        self.assertIn("mcp__isms_t__list_endpoints", prompt)

    def test_errored_server_hidden(self):
        self.srv.status = "error"
        self.assertNotIn("mcp__isms_t__list_endpoints",
                         [s["function"]["name"] for s in mcp.ready_tool_schemas()])
        self.assertNotIn("isms_t", mcp.chat_prompt())


class PersonalServerTests(unittest.TestCase):
    """A personal server's tools reach only its owner; a global name wins on a clash."""

    def setUp(self):
        from core import request_context
        self.rc = request_context
        self.tool = {"name": "get_issue", "description": "Get a Jira issue."}
        self.srv = mcp.McpServer("jira_p", {"command": "uvx"}, owner=5)
        self.srv.status, self.srv.tools = "ready", [self.tool]
        mcp._servers[self.srv.key] = self.srv
        mcp.registry.register("mcp__jira_p__get_issue", mcp._tool_bridge(self.srv, "get_issue"),
                              mcp._bridge_schema("jira_p", self.tool), source=f"mcp:{self.srv.key}",
                              meta={}, replace=True, owner=5)

    def tearDown(self):
        mcp.disconnect_one("jira_p", 5)
        mcp.disconnect_one("jira_p")
        self.rc.set_current_user(None)

    def _names(self):
        return [s["function"]["name"] for s in mcp.ready_tool_schemas()]

    def test_owner_only(self):
        self.assertEqual(self.srv.key, "jira_p@u5")
        self.rc.set_current_user(5)
        self.assertIn("mcp__jira_p__get_issue", self._names())
        self.assertIsNotNone(mcp.registry.get("mcp__jira_p__get_issue"))
        self.assertIn("`jira_p`", mcp.chat_prompt())
        self.rc.set_current_user(6)
        self.assertNotIn("mcp__jira_p__get_issue", self._names())
        self.assertIsNone(mcp.registry.get("mcp__jira_p__get_issue"))
        self.assertNotIn("jira_p", mcp.chat_prompt())
        self.assertNotIn("mcp__jira_p__get_issue", [t["function"]["name"] for t in mcp.registry.schemas()])

    def test_global_name_wins(self):
        glob = mcp.McpServer("jira_p", {"command": "npx"})
        glob.status, glob.tools = "ready", [self.tool]
        mcp._servers["jira_p"] = glob
        mcp.registry.register("mcp__jira_p__get_issue", mcp._tool_bridge(glob, "get_issue"),
                              mcp._bridge_schema("jira_p", self.tool), source="mcp:jira_p", meta={}, replace=True)
        self.rc.set_current_user(5)
        self.assertEqual(mcp.registry.get("mcp__jira_p__get_issue").owner, None)
        self.assertEqual(self._names().count("mcp__jira_p__get_issue"), 1)

    def test_status_scoped(self):
        self.assertEqual([s["scope"] for s in mcp.mcp_status(5) if s["name"] == "jira_p"], ["user"])
        self.assertNotIn("jira_p", [s["name"] for s in mcp.mcp_status(6)])
        self.assertNotIn("jira_p", [s["name"] for s in mcp.mcp_status()])

    def test_secret_ref_per_user(self):
        self.assertEqual(mcp.secret_env_ref("jira_p", "TOKEN"), "jira_p:env:TOKEN")
        self.assertEqual(mcp.secret_env_ref("jira_p", "TOKEN", 5), "u5:jira_p:env:TOKEN")

    def test_disconnect_personal_drops_tools(self):
        mcp.disconnect_one("jira_p", 5)
        self.rc.set_current_user(5)
        self.assertIsNone(mcp.registry.get("mcp__jira_p__get_issue"))


class PanMaskTests(unittest.TestCase):
    def test_tool_output_pan_masked(self):
        out = mcp._stringify_content({"content": [{"type": "text", "text": f"txn card {VISA_TEST} ok"}]})
        self.assertNotIn(VISA_TEST, out)
        self.assertIn("****1111", out)


if __name__ == "__main__":
    unittest.main()
