"""core/acp/protocol.py - SPIKE (P6-A): Agent Client Protocol framing, pure.

ACP (Zed's Agent Client Protocol, newline-delimited JSON-RPC 2.0 over stdio)
is how editors drive coding agents. This module is the protocol layer ONLY:
no process, no model, no network. `AcpConnection` consumes parsed request
frames and emits response + session/update notification frames; the `backend`
injected at construction supplies the actual agent behavior
(scripts/acp_agent.py wires the run_subagent bridge; the probe uses a
scripted backend).

Agent-side methods implemented:
  initialize      echo the client's protocolVersion + agentCapabilities
  session/new     fresh UUID sessionId, cwd/mcpServers recorded privately
  session/prompt  text blocks joined; the backend streams update items through
                  an async on_update callback (agent_message_chunk /
                  tool_call / tool_call_update), which surface as
                  session/update notifications BEFORE the stopReason response
  session/cancel  notification, no response; cooperative - it is checked at
                  the START of the next prompt on that session (a mid-prompt
                  cancel of a running run_subagent loop is a wiring-decision
                  problem, not a spike problem)

The layer itself is synchronous. Backends may be async (run_subagent is) -
their coroutines are driven with asyncio.run() from this thread; nesting that
inside a running loop raises a clear error instead of corrupting the loop.

Rollback: delete this package, the script, and its tests; nothing imports it.
"""

import asyncio
import inspect
import json
import uuid

JSONRPC_PARSE_ERROR = -32700
METHOD_NOT_FOUND = -32601
INVALID_SESSION = -32602


def parse_lines(text: str) -> list:
    """One JSON object per line; blank lines and unparsable lines -> None."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            out.append(None)
    return out


def _text_of(blocks) -> str:
    """ACP prompt blocks -> plain text (text blocks joined with newlines)."""
    parts = []
    for b in blocks or []:
        if isinstance(b, dict) and b.get("type") == "text":
            parts.append(str(b.get("text") or ""))
    return "\n".join(p for p in parts if p)


def _rpc_error(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id,
            "error": {"code": code, "message": message}}


_INVALID_SESSION = "__error__"   # handler marker: params don't name a session


class AcpConnection:
    """Frame handler: feed requests, get back a list of outgoing frames
    (the response plus, for prompts, every session/update push in order)."""

    def __init__(self, backend):
        self.backend = backend
        self.sessions = []           # sessionId strings, creation order
        self._meta = {}              # sessionId -> {"cwd":..., "mcpServers":[...]}
        self._cancelled = set()      # sessionIds cancelled between prompts

    def handle_frames(self, frames: list) -> list:
        out = []
        for frame in frames:
            if frame is None:
                out.append(_rpc_error(None, JSONRPC_PARSE_ERROR, "parse error"))
                continue
            if not isinstance(frame, dict) or "method" not in frame:
                mid = frame.get("id") if isinstance(frame, dict) else None
                out.append(_rpc_error(mid, JSONRPC_PARSE_ERROR, "not a request"))
                continue
            out.extend(self._handle(frame))
        return out

    def _handle(self, frame: dict) -> list:
        method, msg_id = frame["method"], frame.get("id")
        params = frame.get("params") or {}
        handler = getattr(self, "_on_" + method.replace("/", "_"), None)
        if handler is None:
            if msg_id is not None:
                return [_rpc_error(msg_id, METHOD_NOT_FOUND, f"unknown method {method}")]
            return []
        if method == "session/cancel":
            handler(params)          # notification: never a response
            return []
        if msg_id is None:
            return []
        result, updates, sid = handler(params)
        if isinstance(result, dict) and _INVALID_SESSION in result:
            return [_rpc_error(msg_id, INVALID_SESSION, result[_INVALID_SESSION])]
        out = []
        if updates:
            out.extend(self._update_frames(sid, updates))
        out.append({"jsonrpc": "2.0", "id": msg_id, "result": result})
        return out

    def _update_frames(self, session_id: str, items: list) -> list:
        frames = []
        for item in items:
            if "tool_call" in item:
                upd = {"sessionUpdate": "tool_call",
                       "toolCallId": item["tool_call"].get("toolCallId", ""),
                       "title": item["tool_call"].get("title", ""),
                       "kind": item["tool_call"].get("kind", "read"),
                       "status": item["tool_call"].get("status", "pending")}
            elif "tool_call_update" in item:
                upd = {"sessionUpdate": "tool_call_update",
                       "toolCallId": item["tool_call_update"].get("toolCallId", ""),
                       "status": item["tool_call_update"].get("status", "completed")}
            else:
                upd = {"sessionUpdate": "agent_message_chunk",
                       "content": item.get("content", {"type": "text", "text": ""})}
            frames.append({"jsonrpc": "2.0", "method": "session/update",
                           "params": {"sessionId": session_id, "update": upd}})
        return frames

    # ---- handlers: (result_body, updates, update_session_id) ---------------

    def _on_initialize(self, params: dict) -> tuple:
        result = {
            "protocolVersion": params.get("protocolVersion", 4),
            "agentCapabilities": {"loadSession": False,
                                  "promptCapabilities": {"audio": False,
                                                         "embeddedContext": True}},
            "authMethods": [],
        }
        return result, [], None

    def _on_session_new(self, params: dict) -> tuple:
        cwd = params.get("cwd", "")
        new_fn = getattr(self.backend, "new_session", None)
        if new_fn is None:
            sid = f"acp-{uuid.uuid4().hex[:12]}"
        elif inspect.iscoroutinefunction(new_fn):
            sid = asyncio.run(new_fn(cwd))
        else:
            sid = new_fn(cwd)
        self.sessions.append(sid)
        self._meta[sid] = {"cwd": cwd, "mcpServers": params.get("mcpServers", [])}
        return {"sessionId": sid}, [], None

    def _on_session_prompt(self, params: dict) -> tuple:
        sid = params.get("sessionId", "")
        if sid not in self.sessions:
            return ({_INVALID_SESSION: f"unknown session {sid!r}"}, [], None)
        if sid in self._cancelled:
            self._cancelled.discard(sid)
            return {"stopReason": "cancelled"}, [], None
        updates = []

        async def on_update(item) -> None:
            if "tool_call" in item:
                updates.append({"tool_call": item["tool_call"]})
            elif "tool_call_update" in item:
                updates.append({"tool_call_update": item["tool_call_update"]})
            else:
                updates.append({"content": item.get("content",
                                                   {"type": "text", "text": ""})})

        try:
            text = _text_of(params.get("prompt"))
            if inspect.iscoroutinefunction(self.backend.prompt):
                stop = asyncio.run(self.backend.prompt(sid, text, on_update))
            else:
                stop = self.backend.prompt(sid, text, on_update)
        except RuntimeError as e:    # nested inside a running loop
            raise ValueError(f"ACP prompt cannot run inside an event loop: {e}") from e
        except Exception as e:       # noqa: BLE001 - surfaces as stopReason=error
            return {"stopReason": "error", "_error": str(e)}, updates, sid
        return {"stopReason": stop or "end_turn"}, updates, sid

    def _on_session_cancel(self, params: dict) -> None:
        sid = params.get("sessionId", "")
        if sid in self.sessions:
            self._cancelled.add(sid)
