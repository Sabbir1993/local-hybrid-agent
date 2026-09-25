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
import hashlib
import json
import secrets
import sys
import time
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from . import auth_db
from .audit import audit_log
from .auth import Principal, SESSION_COOKIE, user_has_permission, verify_session
from .deps import get_current_user
from .request_context import normalize_device_id

DEVICE_KEY_PREFIX = "a770_dev_"

router = APIRouter()

# user_id -> WebSocket
_connections: dict[int, WebSocket] = {}
# user_id -> {req_id: asyncio.Future}
_pending: dict[int, dict[str, asyncio.Future]] = {}
# user_id -> {"hostname": str, "connected_at": float}
_meta: dict[int, dict] = {}

# user_id -> time the last companion socket closed (for the reconnect grace window)
_last_seen: dict[int, float] = {}

DEFAULT_TIMEOUT_S = 60
RECONNECT_GRACE_S = 10    # a companion that dropped this recently is expected back
CONNECT_WAIT_S = 5        # how long call() waits for a (re)connection before failing
# ops that are safe to repeat after a dropped connection; writes and shell are not
READ_ONLY_OPS = {"fs.read", "fs.read_b64", "fs.list", "fs.grep", "fs.tree", "fs.browse"}


def is_connected(user_id: Optional[int]) -> bool:
    return user_id is not None and user_id in _connections


def is_available(user_id: Optional[int], grace: float = RECONNECT_GRACE_S) -> bool:
    """Connected, or disconnected so recently that a reconnect is expected --
    lets a tool ride out the companion's brief reconnect instead of failing."""
    if is_connected(user_id):
        return True
    t = _last_seen.get(user_id) if user_id is not None else None
    return t is not None and time.time() - t < grace


async def _wait_connected(user_id: int, wait_s: float) -> Optional[WebSocket]:
    deadline = time.monotonic() + wait_s
    while True:
        ws = _connections.get(user_id)
        if ws is not None or time.monotonic() >= deadline:
            return ws
        await asyncio.sleep(0.25)


def connection_info(user_id: Optional[int]) -> Optional[dict]:
    if not is_connected(user_id):
        return None
    return dict(_meta.get(user_id) or {})


async def call(user_id: int, op: str, params: dict, timeout: float = DEFAULT_TIMEOUT_S) -> dict:
    """Send an RPC frame to the user's companion and await its result frame.

    Waits briefly for a reconnecting companion, and repeats a read-only op once
    if the connection drops mid-call. Raises ConnectionError if no companion
    comes back, or the frame's own "error" on failure (caller renders that as
    the tool's error string).
    """
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
                              "check the A770 Companion app on your machine, then retry the same call")

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
    from .small_model import APP_CONFIG
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


@router.websocket("/ws/companion")
async def companion_socket(ws: WebSocket):
    # Browsers always send Origin on a WebSocket handshake; the companion (node `ws`)
    # never does. Refusing any Origin keeps a web page -- XSS, or another app on a
    # sibling port that SameSite doesn't separate -- from posing as the companion
    # and feeding the agent fake file contents / shell output.
    if ws.headers.get("origin"):
        await ws.close(code=4403)
        return
    user_id, device_row = await _authenticate(ws)
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
        # calls in flight were sent on the old socket; the new one will never
        # answer them -- fail them now (read-only ops get retried by call())
        for fut in _pending.pop(user_id, {}).values():
            if not fut.done():
                fut.set_exception(ConnectionError(
                    "companion connection was interrupted - retry the same call"))

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
                dev = normalize_device_id(str(frame.get("device_id") or "")) or None
                if device_row is not None and dev != device_row["device_hash"]:
                    # a device key only works on the machine it was paired on
                    await ws.close(code=4403)
                    return
                _meta[user_id] = {
                    "hostname": str(frame.get("hostname") or "unknown"),
                    "device_id": dev,
                    "device_row_id": device_row["id"] if device_row is not None else None,
                    "connected_at": time.time(),
                }
                if device_row is not None:
                    auth_db.touch_companion_device(device_row["id"])
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
            _last_seen[user_id] = time.time()
            for fut in _pending.pop(user_id, {}).values():
                if not fut.done():
                    fut.set_exception(ConnectionError(
                        "companion connection was interrupted - retry the same call"))


# ---------------- device pairing ----------------

class PairReq(BaseModel):
    device_id: str = Field(min_length=3, max_length=200)
    name: str = Field(default="", max_length=80)


@router.post("/companion/pair")
async def pair_device(req: PairReq, request: Request, user: Principal = Depends(get_current_user)):
    """Called by the companion right after the user signs in inside it: returns a device
    key (shown once) that replaces the browser session on the companion socket."""
    if user.via_token:
        raise HTTPException(status_code=403, detail="API tokens cannot pair devices")
    dev = normalize_device_id(req.device_id.strip())
    auth_db.revoke_companion_devices(user.id, dev)      # re-pairing retires older keys
    raw = DEVICE_KEY_PREFIX + secrets.token_urlsafe(32)
    name = req.name.strip() or "Companion"
    rid = auth_db.create_companion_device(user.id, name, dev, _key_hash(raw))
    audit_log(user, action="companion.pair", resource=f"device:{rid}", result="allow",
              ip=request.client.host if request.client else None, detail={"name": name})
    return {"id": rid, "device_key": raw}


@router.get("/companion/devices")
async def list_devices(all: bool = False, user: Principal = Depends(get_current_user)):
    if all:
        if not user_has_permission(user, "users.manage"):
            raise HTTPException(status_code=403, detail="missing permission: users.manage")
        rows = auth_db.list_companion_devices()
    else:
        rows = auth_db.list_companion_devices(user.id)
    live = {m.get("device_row_id") for m in _meta.values()}
    for r in rows:
        r["connected"] = r["id"] in live
    return {"devices": rows}


@router.delete("/companion/devices/{device_row_id}")
async def revoke_device(device_row_id: int, request: Request, user: Principal = Depends(get_current_user)):
    row = auth_db.get_companion_device(device_row_id)
    if not row or (row["user_id"] != user.id and not user_has_permission(user, "users.manage")):
        raise HTTPException(status_code=404, detail="device not found")
    auth_db.revoke_companion_device(device_row_id)
    # drop its live socket right away
    uid = row["user_id"]
    ws = _connections.get(uid)
    if ws is not None and (_meta.get(uid) or {}).get("device_row_id") == device_row_id:
        try:
            await ws.close(code=4401)
        except Exception:
            pass
    audit_log(user, action="companion.revoke", resource=f"device:{device_row_id}", result="allow",
              ip=request.client.host if request.client else None)
    return {"ok": True}
