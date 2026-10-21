"""scripts/acp_agent.py - P6-A spike: this agent over the Agent Client Protocol.

A stdio process an ACP client (e.g. Zed) can drive:

    python scripts/acp_agent.py             # run_subagent bridge (needs a live model)
    python scripts/acp_agent.py --echo      # scripted backend (no model, offline)

Newline-delimited JSON-RPC 2.0 in on stdin, out on stdout. The SPIKE cold
start is minimal: initialize -> session/new -> session/prompt (text blocks)
with session/update pushes streamed before the stopReason response.

BRIDGE (and its spike-level limits, recorded for the wiring decision):
- the backend is core.subagent.runner.run_subagent in TEXT-ONLY mode
  (tool_allowlist=[] to keep the file tools away from the companion bridge, and
  role=None so the plain executor lane answers) - the FINAL reply becomes ONE
  agent_message_chunk; incremental step/tool-call streaming is a wiring
  decision, it does not exist in the subagent runner's return contract,
- cancellation is cooperative (checked between prompts), and
- the adapter process talks to the machine's model server directly - no HTTP,
  no user auth: whoever runs the process IS the operator on this machine.

Rollback: delete this file plus core/acp/ and tests/test_acp_adapter.py.
"""

import inspect
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.acp.protocol import AcpConnection, parse_lines  # noqa: E402


class SubagentBackend:
    """One prompt = one run_subagent task; the final reply is a single chunk."""

    def __init__(self):
        self.sessions = 0

    async def prompt(self, session_id: str, text: str, on_update) -> str:
        await on_update({"tool_call": {"toolCallId": f"{session_id}-task",
                                       "title": text[:48] or (text or "task"),
                                       "kind": "think", "status": "in_progress"}})
        from core.subagent.runner import run_subagent
        reply = await run_subagent(text, role=None, tool_allowlist=[], max_steps=10)
        await on_update({"tool_call_update": {"toolCallId": f"{session_id}-task",
                                              "status": "completed"}})
        await on_update({"content": {"type": "text", "text": str(reply)}})
        return "end_turn"


class EchoBackend:
    """Offline backend for the probe: no model, just a round-trip proof."""

    def __init__(self):
        self.turns = 0

    async def prompt(self, session_id: str, text: str, on_update) -> str:
        self.turns += 1
        await on_update({"content": {"type": "text", "text": f"echo-{self.turns}: "}})
        await on_update({"tool_call_update": {"toolCallId": f"{session_id}-turn",
                                              "status": "completed"}})
        await on_update({"content": {"type": "text", "text": text}})
        return "end_turn"


def main() -> int:
    echo = "--echo" in sys.argv[1:]
    backend = EchoBackend() if echo else SubagentBackend()
    conn = AcpConnection(backend=backend)
    # NOTE: `for line in sys.stdin` read-ahead buffers and stalls a pipe
    # client; readline() stays unbuffered per line.
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        for out in conn.handle_frames(parse_lines(line)):
            if out is not None:
                sys.stdout.write(json.dumps(out) + "\n")
                sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
