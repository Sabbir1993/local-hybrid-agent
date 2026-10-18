import asyncio
from typing import Optional
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core.auth import SESSION_COOKIE, verify_session, user_has_permission, Principal
from core.telemetry import get_telemetry_snapshot

router = APIRouter(tags=["telemetry"])


def _authenticate_ws(ws: WebSocket) -> Optional[Principal]:
    token = ws.cookies.get(SESSION_COOKIE)
    if not token:
        auth_hdr = ws.headers.get("authorization") or ""
        token = auth_hdr[7:].strip() if auth_hdr[:7].lower() == "bearer " else ""
    if not token:
        return None
    return verify_session(token, touch=False)


@router.websocket("/ws/telemetry")
async def telemetry_websocket(ws: WebSocket):
    # 1. Authenticate user from session cookie or token
    principal = _authenticate_ws(ws)
    if not principal:
        await ws.close(code=4401)
        return

    # 2. RBAC check for monitor.view permission
    if not user_has_permission(principal, "monitor.view"):
        await ws.close(code=4403)
        return

    await ws.accept()

    async def recv_loop():
        try:
            while True:
                # Keep socket alive and handle any ping/control frames from client
                await ws.receive_text()
        except (WebSocketDisconnect, ConnectionResetError, asyncio.CancelledError):
            pass

    async def send_loop():
        try:
            # Immediate snapshot on connect
            snapshot = await get_telemetry_snapshot()
            await ws.send_json(snapshot)

            # Continuous real-time stream every 750ms
            while True:
                await asyncio.sleep(0.75)
                snapshot = await get_telemetry_snapshot()
                await ws.send_json(snapshot)
        except (WebSocketDisconnect, ConnectionResetError, asyncio.CancelledError):
            pass

    t_recv = asyncio.create_task(recv_loop())
    t_send = asyncio.create_task(send_loop())

    done, pending = await asyncio.wait([t_recv, t_send], return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
