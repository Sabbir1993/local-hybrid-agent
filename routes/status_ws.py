import asyncio
from typing import Optional
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core.auth import SESSION_COOKIE, API_TOKEN_PREFIX, verify_session, verify_api_token, Principal
from routes.control.status_endpoints import get_system_status

router = APIRouter(tags=["status_ws"])


def _authenticate_ws(ws: WebSocket) -> Optional[Principal]:
    token = ws.cookies.get(SESSION_COOKIE)
    if not token:
        auth_hdr = ws.headers.get("authorization") or ""
        token = auth_hdr[7:].strip() if auth_hdr[:7].lower() == "bearer " else ""
    if not token:
        token = ws.query_params.get("token") or ws.query_params.get("api_key") or ""
    if not token:
        return None
    if token.startswith(API_TOKEN_PREFIX):
        return verify_api_token(token)
    return verify_session(token, touch=False)


@router.websocket("/ws/status")
async def status_websocket(ws: WebSocket):
    principal = _authenticate_ws(ws)
    if not principal:
        await ws.close(code=4401)
        return

    await ws.accept()

    refresh_event = asyncio.Event()

    async def recv_loop():
        try:
            while True:
                msg = await ws.receive_text()
                if msg:
                    # Client requested an immediate status refresh or sent keepalive ping
                    refresh_event.set()
        except (WebSocketDisconnect, ConnectionResetError, asyncio.CancelledError):
            pass

    async def send_loop():
        try:
            # Immediate snapshot on connect
            snapshot = await get_system_status(principal)
            await ws.send_json(snapshot)

            while True:
                # Push every 2.5s or immediately when triggered by client action
                try:
                    await asyncio.wait_for(refresh_event.wait(), timeout=2.5)
                    refresh_event.clear()
                except asyncio.TimeoutError:
                    pass
                snapshot = await get_system_status(principal)
                await ws.send_json(snapshot)
        except (WebSocketDisconnect, ConnectionResetError, asyncio.CancelledError):
            pass

    t_recv = asyncio.create_task(recv_loop())
    t_send = asyncio.create_task(send_loop())

    done, pending = await asyncio.wait([t_recv, t_send], return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
