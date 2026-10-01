import json
import secrets
import sys
import time
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from .. import auth_db
from ..audit import audit_log
from ..auth import Principal, user_has_permission
from ..deps import get_current_user
from ..request_context import normalize_device_id
from .constants import (
    DEVICE_KEY_PREFIX,
    _connections,
    _last_seen,
    _meta,
    _pending,
)
from .rpc import _authenticate, _key_hash

def _audit(*args, **kwargs):
    fn = getattr(sys.modules.get("core.companion_bridge"), "audit_log", audit_log)
    return fn(*args, **kwargs)

router = APIRouter()


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
    key (shown once) that replaces the browser session on the companion socket.
    Binding a new device is persistent access: MFA-enrolled users need a verified session."""
    if user.via_token:
        raise HTTPException(status_code=403, detail="API tokens cannot pair devices")
    if not user.mfa_verified and auth_db.totp_enabled(user.id):
        raise HTTPException(status_code=403, detail="mfa_step_up_required")
    dev = normalize_device_id(req.device_id.strip())
    auth_db.revoke_companion_devices(user.id, dev)      # re-pairing retires older keys
    raw = DEVICE_KEY_PREFIX + secrets.token_urlsafe(32)
    name = req.name.strip() or "Companion"
    rid = auth_db.create_companion_device(user.id, name, dev, _key_hash(raw))
    _audit(user, action="companion.pair", resource=f"device:{rid}", result="allow",
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
    _audit(user, action="companion.revoke", resource=f"device:{device_row_id}", result="allow",
           ip=request.client.host if request.client else None)
    return {"ok": True}
