import asyncio
import hashlib
import json
import sys
import time
import uuid
from typing import Optional
from fastapi import WebSocket
from .. import auth_db
from ..auth import SESSION_COOKIE, verify_session
from .constants import (
    CONNECT_WAIT_S,
    DEFAULT_TIMEOUT_S,
    DEVICE_KEY_PREFIX,
    CONFIRM_OPS,
    READ_ONLY_OPS,
    _connections,
    _pending,
)


async def _wait_connected(user_id: int, wait_s: float) -> Optional[WebSocket]:
    deadline = time.monotonic() + wait_s
    while True:
        ws = _connections.get(user_id)
        if ws is not None or time.monotonic() >= deadline:
            return ws
        await asyncio.sleep(0.25)


async def call(user_id: int, op: str, params: dict, timeout: float = DEFAULT_TIMEOUT_S) -> dict:
    """Send an RPC frame to the user's companion and await its result frame.

    Waits briefly for a reconnecting companion, and repeats a read-only op once
    if the connection drops mid-call. Raises ConnectionError if no companion
    comes back, or the frame's own "error" on failure (caller renders that as
    the tool's error string).
    """
    from ..request_context import device_approved
    if op in CONFIRM_OPS and device_approved():
        params = {**params, "approved_in_app": True}      # approved in the web app's card: no second native dialog
    try:
        return await _call_once(user_id, op, params, timeout)
    except ConnectionError:
        if op not in READ_ONLY_OPS:
            raise
        return await _call_once(user_id, op, params, timeout)


async def _call_once(user_id: int, op: str, params: dict, timeout: float) -> dict:
    ws = await _wait_connected(user_id, CONNECT_WAIT_S)
    if ws is None:
        raise ConnectionError("companion connection was interrupted and did not come back - "
                              "check the SSL Local Agent app on your machine, then retry the same call")

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


def _key_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _require_pairing() -> bool:
    from ..small_model import APP_CONFIG
    return bool((APP_CONFIG.get("security") or {}).get("companion_require_pairing", False))


async def _authenticate(ws: WebSocket) -> tuple:
    """-> (user_id, device_row | None). A paired companion authenticates with its own
    device key (Bearer a770_dev_...), bound to one machine and revocable on its own.
    Unpaired companions may still use the browser session for now (migration) unless
    security.companion_require_pairing is set. Never a query parameter: URLs (and so
    tokens in them) land in proxy / access logs."""
    auth_hdr = ws.headers.get("authorization") or ""
    bearer = auth_hdr[7:].strip() if auth_hdr[:7].lower() == "bearer " else ""
    if bearer.startswith(DEVICE_KEY_PREFIX):
        row = auth_db.get_companion_device_by_key(_key_hash(bearer))
        if not row or row["revoked_at"] is not None:
            return None, None
        user = auth_db.get_user_by_id(row["user_id"])
        if not user or not user["is_active"]:
            return None, None
        return row["user_id"], row
    if _require_pairing():
        return None, None
    principal = verify_session(ws.cookies.get(SESSION_COOKIE) or bearer)
    if principal:
        print(f"[companion] user {principal.username} connected with a session token - "
              "pair the device (rebuilt companion does this automatically)", file=sys.stderr)
    return (principal.id if principal else None), None
