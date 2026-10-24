import asyncio
from urllib.parse import urlsplit

from fastapi import Depends, Request
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


_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


def _same_machine_base(request: Request) -> str:
    """http://127.0.0.1:<port> when the person is using this app ON the machine that runs it (the TCP peer is loopback AND
    the page was opened through a loopback host name): the provider can redirect straight back to this server, so no
    companion app and no public https address are needed. Empty for any remote browser, so a remote user still gets the
    clear 'open the companion' message."""
    peer = request.client.host if request.client else ""
    host = request.headers.get("host", "")
    try:
        name = urlsplit("//" + host).hostname or ""
    except ValueError:
        return ""
    return f"http://{host}" if peer in _LOOPBACK and name in _LOOPBACK else ""


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
async def oauth_start(name: str, req: OAuthStartReq, request: Request, user: Principal = Depends(_use)):
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
    base = _redirect_base() or _same_machine_base(request)
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
async def oauth_callback(request: Request):
    """Where the provider sends the browser back to. The session cookie is SameSite=Strict, so a navigation that
    starts on the provider's site arrives WITHOUT it and /oauth/finish would answer 'not authenticated'. This landing
    page holds no data and does nothing itself: it forwards the same parameters to /oauth/finish from this site, a
    same-site navigation that carries the cookie. The sign-in is still completed only for the signed-in owner."""
    import html as _html
    from urllib.parse import urlencode
    keep = {k: v for k, v in request.query_params.items() if k in ("state", "code", "error")}
    url = _html.escape(f"{request.url.path.rsplit('/', 1)[0]}/finish?{urlencode(keep)}", quote=True)
    return HTMLResponse(f'<!doctype html><meta charset=utf-8><title>Signing in</title>'
                        f'<meta http-equiv="refresh" content="0;url={url}">'
                        f'<body style="font:15px system-ui;padding:40px">Finishing sign-in... '
                        f'<a href="{url}">Continue</a></body>')


@router.get("/oauth/finish", response_class=HTMLResponse)
async def oauth_finish(state: str = "", code: str = "", error: str = "", user: Principal = Depends(_use)):
    """Completes the server-side redirect flow for the signed-in user (reached from /oauth/callback)."""
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
