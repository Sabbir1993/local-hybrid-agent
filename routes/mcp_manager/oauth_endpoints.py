import asyncio
from fastapi import Depends
from fastapi.responses import HTMLResponse, JSONResponse
from core.small_model import APP_CONFIG
from core import mcp_oauth
from core import mcp as mcp_core
from core.audit import audit_log
from core.auth import Principal

from .base import MANAGE_PERM, _use, router
from .models import OAuthStartReq
from .store import _bg_tasks, _can_manage_global, _connect_in_background, _owner_for, _servers_for


def _redirect_base() -> str:
    """Public https URL of this app, if the admin set one: enables the server-side callback."""
    return str(APP_CONFIG.get("capabilities", {}).get("mcp_oauth_redirect_base") or "").rstrip("/")


async def _loopback_flow(user_id: int, name: str, owner, scfg: dict) -> None:
    """Companion listens on 127.0.0.1, opens the system browser, returns the code."""
    from core import companion_bridge
    try:
        start = await mcp_oauth.begin(name, owner, scfg)
        res = await companion_bridge.call(user_id, "oauth.loopback", {
            "auth_url": start["auth_url"], "placeholder": mcp_oauth.REDIRECT_PLACEHOLDER,
            "state": start["state"], "port": start["loopback_port"], "timeout_s": 300,
        }, timeout=320)
        if not isinstance(res, dict) or not res.get("code"):
            raise RuntimeError((res or {}).get("error") or "sign-in was cancelled")
        if res.get("state") != start["state"]:
            raise RuntimeError("sign-in state mismatch")
        await mcp_oauth.complete(start["state"], res["code"], res.get("redirect_uri"))
        mcp_oauth.set_status(name, owner, "done")
        await mcp_core.connect_one(name, scfg, owner)
    except Exception as e:
        msg = str(e)
        if "unknown op" in msg:
            msg = ("the companion app on this device is too old for sign-in - install "
                   "companion v0.2.0 or newer, reopen it, then press Connect account again")
        mcp_oauth.set_status(name, owner, "error", msg[:300])


@router.post("/servers/{name}/oauth/start")
async def oauth_start(name: str, req: OAuthStartReq, user: Principal = Depends(_use)):
    owner, resp = _owner_for(req.scope, user)
    if resp:
        return resp
    scfg = _servers_for(owner).get(name)
    if scfg is None:
        return JSONResponse({"error": f"unknown server '{name}'"}, status_code=404)
    if (scfg.get("transport") or ("stdio" if scfg.get("command") else "http")) != "http":
        return JSONResponse({"error": "OAuth sign-in only applies to http servers"}, status_code=400)
    from core import companion_bridge
    audit_log(user, action="mcp.oauth.start", resource=f"mcp:{name}",
              permission_key=MANAGE_PERM if owner is None else "chat.use", detail={"scope": req.scope})
    if companion_bridge.is_connected(user.id):
        mcp_oauth.set_status(name, owner, "pending")
        task = asyncio.create_task(_loopback_flow(user.id, name, owner, scfg))
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)
        return {"mode": "companion", "status": "pending",
                "message": "Finish signing in in the browser window the companion opened."}
    base = _redirect_base()
    if not base:
        return JSONResponse({"error": "Open the companion app on this device to sign in "
                                      "(it receives the sign-in redirect on 127.0.0.1)."}, status_code=409)
    try:
        start = await mcp_oauth.begin(name, owner, scfg, redirect_uri=f"{base}/mcp/oauth/callback")
    except Exception as e:
        return JSONResponse({"error": str(e)[:300]}, status_code=502)
    mcp_oauth.set_status(name, owner, "pending")
    return {"mode": "redirect", "status": "pending", "auth_url": start["auth_url"]}


@router.get("/servers/{name}/oauth/status")
async def oauth_status(name: str, scope: str = "user", user: Principal = Depends(_use)):
    owner, resp = _owner_for(scope, user)
    if resp:
        return resp
    return mcp_oauth.get_status(name, owner)


@router.post("/servers/{name}/oauth/disconnect")
async def oauth_disconnect(name: str, req: OAuthStartReq, user: Principal = Depends(_use)):
    owner, resp = _owner_for(req.scope, user)
    if resp:
        return resp
    mcp_oauth.clear_token(name, owner)
    mcp_oauth.set_status(name, owner, "idle")
    audit_log(user, action="mcp.oauth.disconnect", resource=f"mcp:{name}",
              permission_key=MANAGE_PERM if owner is None else "chat.use", detail={"scope": req.scope})
    return {"ok": True}


_DONE_PAGE = """<!doctype html><meta charset=utf-8><title>Signed in</title>
<body style="font:15px system-ui;padding:40px">{msg}<br><br>You can close this window.</body>"""


@router.get("/oauth/callback", response_class=HTMLResponse)
async def oauth_callback(state: str = "", code: str = "", error: str = "", user: Principal = Depends(_use)):
    """Server-side redirect target (only with capabilities.mcp_oauth_redirect_base)."""
    import html as _html
    if error or not code:
        return HTMLResponse(_DONE_PAGE.format(msg=_html.escape(f"Sign-in failed: {error or 'no code'}")), status_code=400)
    # the flow's owner must be the signed-in user (or a global server started by an admin)
    flow = mcp_oauth._flows.get(state) or {}
    if flow.get("owner") not in (user.id, None) or (flow.get("owner") is None and not _can_manage_global(user)):
        return HTMLResponse(_DONE_PAGE.format(msg="This sign-in belongs to another account."), status_code=403)
    try:
        done = await mcp_oauth.complete(state, code)
    except Exception as e:
        return HTMLResponse(_DONE_PAGE.format(msg=_html.escape(f"Sign-in failed: {e}")), status_code=400)
    mcp_oauth.set_status(done["name"], done["owner"], "done")
    scfg = _servers_for(done["owner"]).get(done["name"])
    if scfg:
        _connect_in_background(done["name"], scfg, done["owner"])
    return HTMLResponse(_DONE_PAGE.format(msg="Signed in."))
