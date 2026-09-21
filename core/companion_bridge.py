"""core/companion_bridge.py - WebSocket bridge to per-user local companion apps.

A "companion" is the Electron app running on a user's own machine. When
connected, file and shell tool calls for that user are forwarded here instead
of touching the server's local disk/OS (see core/agent_tools.py,
core/shell_tools.py, routes/projects.py for the call sites).

Keyed by user_id, mirroring the existing per-user dicts in core/agent_tools.py
(_active_project, _ws_changes) -- this is a shared multi-user server, so
connections and in-flight requests must never leak across users.
"""

import asyncio
import json
import time
import uuid
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .auth import verify_session, SESSION_COOKIE

router = APIRouter()

# user_id -> WebSocket
_connections: dict[int, WebSocket] = {}
# user_id -> {req_id: asyncio.Future}
_pending: dict[int, dict[str, asyncio.Future]] = {}
# user_id -> {"hostname": str, "connected_at": float}
_meta: dict[int, dict] = {}

DEFAULT_TIMEOUT_S = 60


def is_connected(user_id: Optional[int]) -> bool:
    return user_id is not None and user_id in _connections


def connection_info(user_id: Optional[int]) -> Optional[dict]:
    if not is_connected(user_id):
        return None
    return dict(_meta.get(user_id) or {})


async def call(user_id: int, op: str, params: dict, timeout: float = DEFAULT_TIMEOUT_S) -> dict:
    """Send an RPC frame to the user's companion and await its result frame.

    Raises ConnectionError if no companion is connected, or the frame's own
    "error" on failure (caller renders that as the tool's error string).
    """
    ws = _connections.get(user_id)
    if ws is None:
        raise ConnectionError("no companion connected for this user")

    req_id = uuid.uuid4().hex[:16]
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    _pending.setdefault(user_id, {})[req_id] = fut
    try:
        await ws.send_text(json.dumps({"type": "call", "req_id": req_id, "op": op, "params": params}))
        try:
            result = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"companion did not respond to '{op}' within {timeout}s")
        if not result.get("ok", False):
            raise RuntimeError(result.get("error") or f"companion op '{op}' failed")
        return result.get("data") or {}
    finally:
        _pending.get(user_id, {}).pop(req_id, None)


async def _authenticate(ws: WebSocket) -> Optional[int]:
    """Session cookie is the only source of truth for identity -- a client-
    supplied user_id in the hello frame is never trusted (see hello handling
    below)."""
    token = ws.cookies.get(SESSION_COOKIE)
    principal = verify_session(token)
    return principal.id if principal else None


@router.websocket("/ws/companion")
async def companion_socket(ws: WebSocket):
    user_id = await _authenticate(ws)
    if user_id is None:
        await ws.close(code=4401)
        return

    await ws.accept()

    # A second companion for the same user replaces the first connection
    # rather than stacking (only one machine can be "the" companion at a time).
    old = _connections.get(user_id)
    if old is not None and old is not ws:
        try:
            await old.close(code=4409)
        except Exception:
            pass

    _connections[user_id] = ws
    _pending.setdefault(user_id, {})

    try:
        while True:
            raw = await ws.receive_text()
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                continue

            ftype = frame.get("type")
            if ftype == "hello":
                _meta[user_id] = {
                    "hostname": str(frame.get("hostname") or "unknown"),
                    "connected_at": time.time(),
                }
            elif ftype == "result":
                req_id = frame.get("req_id")
                fut = _pending.get(user_id, {}).get(req_id)
                if fut is not None and not fut.done():
                    fut.set_result({"ok": frame.get("ok", False), "data": frame.get("data"),
                                    "error": frame.get("error")})
    except WebSocketDisconnect:
        pass
    finally:
        if _connections.get(user_id) is ws:
            _connections.pop(user_id, None)
            _meta.pop(user_id, None)
            for fut in _pending.pop(user_id, {}).values():
                if not fut.done():
                    fut.set_exception(ConnectionError("companion disconnected"))
