from typing import Optional
from fastapi import Depends
from fastapi.responses import JSONResponse
from core import credentials, mcp_catalog
from core import mcp as mcp_core
from core.audit import audit_log
from core.auth import Principal

from .base import MANAGE_PERM, _manage, _server_status, router
from .store import _persist_server_entry, _remove_server_entry, _server_cfg


@router.get("/catalog")
async def catalog():
    out = []
    for entry in mcp_catalog.catalog_with_overrides():
        sid = entry["id"]
        live = _server_status(sid)
        out.append({
            "id": sid,
            "name": entry["name"],
            "description": entry["description"],
            "auth_type": (entry.get("auth") or {}).get("type"),
            "configured": bool((entry.get("auth") or {}).get("client_id")),
            "authorized": credentials.has_token(sid),
            "connected": bool(live and live["status"] == "ready"),
            "status": live,
        })
    return {"connectors": out}


@router.get("/registry/search")
async def registry_search(q: Optional[str] = None):
    """Browse-only lookup against the public MCP Registry (no auth wiring)."""
    return await mcp_catalog.fetch_public_registry(q)


@router.post("/{server_id}/authorize/start")
async def authorize_start(server_id: str, user: Principal = Depends(_manage)):
    entry = mcp_catalog.get_entry(server_id)
    if not entry:
        return JSONResponse({"error": f"unknown connector '{server_id}'"}, status_code=404)
    auth = entry.get("auth") or {}
    if auth.get("type") != "device_flow":
        return JSONResponse({"error": f"connector '{server_id}' does not support device-flow auth"}, status_code=400)
    if not auth.get("client_id"):
        return JSONResponse({
            "error": f"connector '{server_id}' has no client_id configured. Set "
                     f"capabilities.mcp_catalog_overrides.{server_id}.client_id in config/app.json "
                     f"(register a GitHub OAuth App with Device Flow enabled - no client secret needed).",
        }, status_code=400)

    from core import oauth_device_flow as device_flow
    try:
        resp = await device_flow.start(auth)
    except Exception as e:
        return JSONResponse({"error": f"failed to start device flow: {e}"}, status_code=502)

    session = device_flow.new_session(server_id, resp["device_code"], resp["interval"], resp["expires_in"])
    return {
        "session": session,
        "user_code": resp["user_code"],
        "verification_uri": resp["verification_uri"],
        "expires_in": resp["expires_in"],
        "interval": resp["interval"],
    }


@router.get("/{server_id}/authorize/poll")
async def authorize_poll(server_id: str, session: str, user: Principal = Depends(_manage)):
    from core import oauth_device_flow as device_flow

    sess = device_flow.get_session(session)
    if not sess or sess["server_id"] != server_id:
        return JSONResponse({"status": "expired"}, status_code=200)

    entry = mcp_catalog.get_entry(server_id)
    auth = entry.get("auth") or {}
    result = await device_flow.poll(auth, sess["device_code"])

    if result["status"] in ("success", "expired", "denied", "error"):
        device_flow.drop_session(session)

    if result["status"] != "success":
        return result

    credentials.set_token(server_id, result["token"])
    _persist_server_entry(server_id, entry)
    audit_log(user, action="mcp.authorize", resource=server_id, permission_key=MANAGE_PERM)
    live = await mcp_core.connect_one(server_id, _server_cfg(entry))
    return {"status": "success", "connected": live}


@router.post("/{server_id}/disable")
async def disable(server_id: str, user: Principal = Depends(_manage)):
    entry = mcp_catalog.get_entry(server_id)
    if not entry:
        return JSONResponse({"error": f"unknown connector '{server_id}'"}, status_code=404)
    mcp_core.disconnect_one(server_id)
    credentials.delete_token(server_id)
    _remove_server_entry(server_id)
    audit_log(user, action="mcp.disable", resource=server_id, permission_key=MANAGE_PERM)
    return {"ok": True}
