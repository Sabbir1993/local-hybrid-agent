import asyncio
import hmac
import time
from typing import Optional
from urllib.parse import parse_qs, quote, urlsplit

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from core.small_model import APP_CONFIG
from core import mcp_oauth
from core import mcp as mcp_core
from core.audit import audit_log
from core.auth import Principal

from .base import MANAGE_PERM, _use, router
from .models import OAuthPasteReq, OAuthStartReq
from .store import _bg_tasks, _can_manage_global, _connect_in_background, _owner_for, _servers_for


def _redirect_base() -> str:
    """Public https URL of this app, if the admin set one: enables the server-side callback."""
    base = str(APP_CONFIG.get("capabilities", {}).get("mcp_oauth_redirect_base") or "").strip().rstrip("/")
    # only the address of the app belongs here: the callback path is added below. A value that already ends in it (copied from
    # the provider's error text) gave /mcp/oauth/callback/mcp/oauth/callback, which the provider cannot match.
    for tail in ("/mcp/oauth/callback", "/oauth/callback", "/mcp"):
        if base.lower().endswith(tail):
            base = base[: -len(tail)].rstrip("/")
    return base


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


# --- manual mode: RFC 8252 loopback redirect + the person hands the code back by hand --------------------------------
# For a remote server with no companion and no public https address (the same idea as `gcloud auth login
# --no-launch-browser`): the provider redirects the browser to http://127.0.0.1:<port>/callback on the PERSON's own
# machine, where nothing listens, so the browser shows "cannot be reached" - and the address bar still holds the code.
# They paste that address back here. mcp_oauth._flows holds the PKCE verifier and the 10 minute expiry; this table adds
# who started the flow and which redirect address the provider was told, so a paste can only finish its own flow.
_manual: dict = {}          # state -> {"user": id, "owner": id|None, "name": str, "redirect": str}
MAX_PASTE = 4096


def _manual_prune() -> None:
    """Forget pending hand-back flows whose mcp_oauth flow expired or was used (mcp_oauth prunes its own table)."""
    for s in [s for s in _manual if s not in mcp_oauth._flows or mcp_oauth._flows[s]["expires"] < time.time()]:
        _manual.pop(s, None)


def _parse_pasted(text: str) -> dict:
    """Pull code / state / error out of what the person pasted: the full redirect address (query or fragment), or the
    bare code. Everything else in it is ignored. Never logs or returns the code except to the caller."""
    text = text.strip()
    out = {"code": "", "state": "", "error": "", "error_description": ""}
    if "=" in text or "://" in text or "?" in text:
        try:
            parts = urlsplit(text)
        except ValueError:
            return out
        q = parse_qs(parts.query)
        q.update({k: v for k, v in parse_qs(parts.fragment).items() if k not in q})
        for k in out:
            out[k] = (q.get(k) or [""])[0]
    else:
        out["code"] = text
    return out


def _manual_flow_for(user_id: int, owner, name: str, state: str) -> Optional[str]:
    """The pending state of THIS user's flow for this server that matches `state` (constant-time compare), or - when the
    person pasted only a code and no state is known - the one pending flow they have for it. None otherwise."""
    mine = [s for s, f in _manual.items() if f["user"] == user_id and f["owner"] == owner and f["name"] == name]
    if not state:
        return mine[0] if len(mine) == 1 else None
    hit = None
    for s in mine:
        if hmac.compare_digest(s.encode(), state.encode()):
            hit = s
    return hit


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
        return await _manual_start(user, name, owner, scfg)
    try:
        start = await mcp_oauth.begin(name, owner, scfg, redirect_uri=f"{base}/mcp/oauth/callback")
    except Exception as e:
        return JSONResponse({"error": str(e)[:300]}, status_code=502)
    mcp_oauth.set_status(name, owner, "pending")
    return {"mode": "redirect", "status": "pending", "auth_url": start["auth_url"]}


async def _manual_start(user: Principal, name: str, owner, scfg: dict):
    """No companion, no public https base, browser not on the server: loopback redirect, person pastes the result back."""
    try:
        start = await mcp_oauth.begin(name, owner, scfg)       # same placeholder machinery the companion flow uses
    except Exception as e:
        return JSONResponse({"error": str(e)[:300]}, status_code=502)
    port = start.get("loopback_port") or mcp_oauth.DEFAULT_DCR_PORT
    redirect = f"http://127.0.0.1:{port}/callback"
    ph = quote(mcp_oauth.REDIRECT_PLACEHOLDER, safe="")
    auth_url = start["auth_url"].replace(ph, quote(redirect, safe=""))
    _manual_prune()
    for s in [s for s, f in _manual.items() if f["user"] == user.id and f["owner"] == owner and f["name"] == name]:
        _manual.pop(s, None)                    # a new attempt replaces the old one
        mcp_oauth._flows.pop(s, None)
    _manual[start["state"]] = {"user": user.id, "owner": owner, "name": name, "redirect": redirect}
    mcp_oauth.set_status(name, owner, "pending")
    return {"mode": "manual", "status": "pending", "auth_url": auth_url, "state": start["state"]}


@router.post("/servers/{name}/oauth/paste")
async def oauth_paste(name: str, req: OAuthPasteReq, user: Principal = Depends(_use)):
    """Finish a manual sign-in from the address the browser ended on (or the bare code). The audit trail and every
    error message carry no code, state or pasted text."""
    owner, resp = _owner_for(req.scope, user)
    if resp:
        return resp
    audit_log(user, action="mcp.oauth.paste", resource=f"mcp:{name}",
              permission_key=MANAGE_PERM if owner is None else "chat.use", detail={"scope": req.scope})
    if len(req.pasted) > MAX_PASTE or len(req.state or "") > 256:
        return JSONResponse({"error": "that is too long to be a sign-in address - copy only the address bar"}, status_code=413)
    scfg = _servers_for(owner).get(name)
    if scfg is None:
        return JSONResponse({"error": f"unknown server '{name}'"}, status_code=404)
    got = _parse_pasted(req.pasted)
    if got["error"]:
        desc = " ".join(got["error_description"].split())[:160]
        return JSONResponse({"error": f"the provider refused the sign-in: {got['error'][:60]}"
                                      + (f" ({desc})" if desc else "")}, status_code=400)
    if not got["code"]:
        return JSONResponse({"error": "no sign-in code found - paste the full address from the address bar"}, status_code=400)
    if got["state"] and req.state and not hmac.compare_digest(got["state"].encode(), req.state.encode()):
        return JSONResponse({"error": "that address belongs to a different sign-in attempt - start again"}, status_code=400)
    _manual_prune()
    state = _manual_flow_for(user.id, owner, name, got["state"] or req.state or "")
    if state is None:
        return JSONResponse({"error": "this sign-in expired, was already used, or is not yours - press Connect account again"},
                            status_code=400)
    flow = _manual.pop(state)                   # single use, even if the provider then refuses the code
    try:
        await mcp_oauth.complete(state, got["code"], flow["redirect"], expect_owner=owner, check_owner=True)
    except Exception as e:
        msg = str(e)[:300]
        mcp_oauth.set_status(name, owner, "error", msg)
        return JSONResponse({"error": msg}, status_code=400)
    mcp_oauth.set_status(name, owner, "done")
    info = await mcp_core.connect_one(name, scfg, owner)
    return {"ok": True, "status": info.get("status"), "error": info.get("error")}


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
