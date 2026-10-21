"""tests/test_acp_adapter.py - P6-A spike: Agent Client Protocol (ACP) framing.

Under test:
- core/acp/protocol.py - pure framing + handler layer (JSON-RPC 2.0 over
  newline-delimited stdio, no process, no network),
- scripts/acp_agent.py - the stdio process that wires the layer to a backend
  (run_subagent bridge; wiring pinned by a scan test).

ACP methods implemented (agent side): initialize, session/new, session/prompt,
session/cancel; updates flow through session/update notifications
(agent_message_chunk, tool_call, tool_call_update).
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.acp import protocol as acp  # noqa: E402


class FakeBackend:
    """Scripted agent backend for the probe and the tests."""

    def __init__(self):
        self.sessions = []
        self.fed = []

    async def new_session(self, cwd: str, **kw) -> str:
        self.sessions.append(cwd)
        return f"sess-{len(self.sessions)}"

    async def prompt(self, session_id: str, text: str, on_update) -> str:
        self.fed.append((session_id, text))
        await on_update({"content": {"type": "text", "text": "chunk-1"}})
        await on_update({"tool_call_update": {"toolCallId": "t1", "status": "completed"}})
        return "end_turn"

    async def cancel(self, session_id: str) -> None:
        pass


def run(conn, frames):
    """Feed parsed frames; the layer is plain synchronous."""
    return conn.handle_frames(frames)


class TestInitialize(unittest.TestCase):
    def setUp(self):
        self.be = FakeBackend()
        self.conn = acp.AcpConnection(backend=self.be)

    def test_echoes_the_clients_version(self):
        out = run(self.conn, [{"id": 1, "method": "initialize",
                               "params": {"protocolVersion": 4, "clientCapabilities": {}}}])
        self.assertEqual(out[0]["id"], 1)
        self.assertNotIn("error", out[0])
        self.assertEqual(out[0]["result"]["protocolVersion"], 4)
        self.assertIn("agentCapabilities", out[0]["result"])
        self.assertIn("promptCapabilities", out[0]["result"]["agentCapabilities"])

    def test_string_versions_pass_through_too(self):
        out = run(self.conn, [{"id": "a", "method": "initialize",
                               "params": {"protocolVersion": "0.4"}}])
        self.assertEqual(out[0]["result"]["protocolVersion"], "0.4")


class TestSessions(unittest.TestCase):
    def setUp(self):
        self.be = FakeBackend()
        self.conn = acp.AcpConnection(backend=self.be)
        run(self.conn, [{"id": 1, "method": "initialize",
                         "params": {"protocolVersion": 4, "clientCapabilities": {}}}])

    def test_session_new_returns_an_id_and_is_unique(self):
        out = run(self.conn, [{"id": 2, "method": "session/new",
                               "params": {"cwd": "E:/x", "mcpServers": []}}])
        sid = out[0]["result"]["sessionId"]
        out2 = run(self.conn, [{"id": 3, "method": "session/new",
                                "params": {"cwd": "E:/x", "mcpServers": []}}])
        self.assertNotEqual(sid, out2[0]["result"]["sessionId"])
        self.assertEqual(self.be.sessions, ["E:/x", "E:/x"])

    def test_prompt_before_any_session_is_refused(self):
        out = run(self.conn, [{"id": 5, "method": "session/prompt",
                               "params": {"sessionId": "nope", "prompt": []}}])
        self.assertIn("error", out[0])
        self.assertEqual(self.be.fed, [])

    def test_prompt_streams_updates_before_stopping(self):
        run(self.conn, [{"id": 2, "method": "session/new",
                         "params": {"cwd": "E:/x", "mcpServers": []}}])
        out = run(self.conn, [{"id": 6, "method": "session/prompt",
                               "params": {"sessionId": self.conn.sessions[0],
                                          "prompt": [{"type": "text", "text": "hi"}]}}])
        self.assertEqual(out[-1]["result"]["stopReason"], "end_turn")
        updates = [f for f in out if "method" in f and f["method"] == "session/update"]
        self.assertEqual(len(updates), 2)
        self.assertEqual(updates[0]["params"]["update"]["sessionUpdate"],
                         "agent_message_chunk")
        self.assertEqual(updates[1]["params"]["update"]["sessionUpdate"],
                         "tool_call_update")
        self.assertEqual(updates[0]["params"]["sessionId"], self.conn.sessions[0])

    def test_prompt_text_extraction(self):
        run(self.conn, [{"id": 2, "method": "session/new",
                         "params": {"cwd": "E:/x", "mcpServers": []}}])
        run(self.conn, [{"id": 7, "method": "session/prompt",
                         "params": {"sessionId": self.conn.sessions[0],
                                    "prompt": [{"type": "text", "text": "do it"}]}}])
        self.assertEqual(self.be.fed[-1][1], "do it")

    def test_prompt_without_text_only_blocks(self):
        run(self.conn, [{"id": 2, "method": "session/new",
                         "params": {"cwd": "E:/x", "mcpServers": []}}])
        run(self.conn, [{"id": 8, "method": "session/prompt",
                               "params": {"sessionId": self.conn.sessions[0],
                                          "prompt": []}}])
        self.assertEqual(self.be.fed[-1][1], "")   # empty is fine (no text parts)


class TestFraming(unittest.TestCase):
    def test_parse_lines(self):
        self.assertEqual(
            acp.parse_lines('{"id":1,"method":"x"}\n\n{broken\n'),
            [{"id": 1, "method": "x"}, None])
        self.assertEqual(acp.parse_lines('{"no_method_here":true}\n'),
                         [{"no_method_here": True}])   # valid, but no method
        self.assertEqual(acp.parse_lines(""), [])

    def test_unknown_method_is_a_jsonrpc_error(self):
        be = FakeBackend()
        conn = acp.AcpConnection(backend=be)
        out = run(conn, [{"id": 9, "method": "no/such", "params": {}}])
        self.assertEqual(out[0]["error"]["code"], -32601)

    def test_notification_without_id_never_errors(self):
        be = FakeBackend()
        conn = acp.AcpConnection(backend=be)
        out = run(conn, [{"method": "initialize", "params": {}}])   # missing id
        self.assertEqual(out, [])


class TestStdioWiring(unittest.TestCase):
    def test_script_wires_subagent_backend(self):
        src = (Path(__file__).resolve().parents[1] / "scripts" / "acp_agent.py")
        self.assertTrue(src.is_file())
        text = src.read_text(encoding="utf-8")
        self.assertIn("run_subagent", text)
        self.assertIn("SubagentBackend", text)
        self.assertIn("sys.stdin", text)


if __name__ == "__main__":
    unittest.main()
