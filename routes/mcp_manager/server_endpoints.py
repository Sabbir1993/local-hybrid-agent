import json
from typing import Optional
from fastapi import Depends, Query
from fastapi.responses import JSONResponse
from core.config import CONFIG_FILE, write_app_config
from core.small_model import APP_CONFIG
from core import credentials, mcp_oauth
from core import mcp as mcp_core
from core import auth_db
from core.audit import audit_log
from core.auth import Principal

from .base import MANAGE_PERM, _manage, _use, router
from .models import McpImportReq, McpServerReq, UserPackagesReq
from .store import (
    _can_manage_global,
    _check_new,
    _connect_in_background,
    _owner_for,
    _public_cfg,
    _restricted,
    _save_server,
    _servers_for,
    _write_server,
)
from .validation import (
    _PKG_RX,
    _USER_COMMANDS,
    _allowed_commands,
    _package_base,
    _user_packages,
    _validate,
)


@router.get("/servers")
async def list_servers(user: Principal = Depends(_use)):
    """Global servers (admins only - their config is org-wide) plus the caller's personal ones."""
    can_global = _can_manage_global(user)
    out = [_public_cfg(n, s, "global") for n, s in mcp_core.configured_servers(APP_CONFIG).items()] if can_global else []
    out += [_public_cfg(n, s, "user", user.id) for n, s in mcp_core.user_servers(user.id).items()]
    return {
        "servers": out,
        "can_manage_global": can_global,
        "allowed_commands": _allowed_commands() if can_global else list(_USER_COMMANDS),
        "user_allowed_packages": _user_packages(),
    }


@router.post("/servers")
async def add_server(req: McpServerReq, user: Principal = Depends(_use)):
    owner, resp = _owner_for(req.scope, user)
    if resp:
        return resp
    err, code = _check_new(req, user, owner)
    if err:
        return JSONResponse({"error": err}, status_code=code)
    return await _save_server(req, user, "mcp.server.add", [], owner)


@router.put("/servers/{name}")
async def update_server(name: str, req: McpServerReq, from_scope: Optional[str] = Query(None), user: Principal = Depends(_use)):
    req.name = name
    orig_scope = from_scope or req.from_scope
    if not orig_scope:
        check_owner, _ = _owner_for(req.scope, user)
        if check_owner is not None and name in _servers_for(check_owner):
            orig_scope = req.scope
        elif check_owner is None and name in _servers_for(None):
            orig_scope = "global"
        else:
            alt_scope = "global" if req.scope == "user" else "user"
            alt_owner, _ = _owner_for(alt_scope, user)
            if alt_owner is not None and name in _servers_for(alt_owner):
                orig_scope = "user"
            elif alt_owner is None and name in _servers_for(None):
                orig_scope = "global"
            else:
                orig_scope = req.scope

    old_owner, resp = _owner_for(orig_scope, user)
    if resp:
        return resp
    new_owner, resp = _owner_for(req.scope, user)
    if resp:
        return resp

    err = _validate(req, _restricted(user, new_owner))
    if err:
        return JSONResponse({"error": err}, status_code=400)

    existing = _servers_for(old_owner).get(name)
    if existing is None:
        return JSONResponse({"error": f"unknown server '{name}'"}, status_code=404)

    if new_owner != old_owner:
        if name in _servers_for(new_owner):
            return JSONResponse({"error": f"server '{name}' already exists in target scope"}, status_code=409)

    old_keys = existing.get("secret_env_keys") or []
    keep = [k for k in old_keys if k in req.secret_env and not str(req.secret_env[k])]

    if new_owner != old_owner:
        for k in keep:
            val = credentials.get_token(mcp_core.secret_env_ref(name, k, old_owner))
            if val is not None:
                credentials.set_token(mcp_core.secret_env_ref(name, k, new_owner), val)
        for k in old_keys:
            credentials.delete_token(mcp_core.secret_env_ref(name, k, old_owner))
        mcp_core.disconnect_one(name, old_owner)
        mcp_oauth.clear_token(name, old_owner)      # sign in again under the new scope
        if old_owner is None:
            _write_server(name, None)
        else:
            auth_db.delete_user_mcp_server(old_owner, name)
    else:
        for k in old_keys:
            if k not in req.secret_env:
                credentials.delete_token(mcp_core.secret_env_ref(name, k, old_owner))

    return await _save_server(req, user, "mcp.server.update", keep, new_owner)


@router.delete("/servers/{name}")
async def delete_server(name: str, scope: str = "global", user: Principal = Depends(_use)):
    owner, resp = _owner_for(scope, user)
    if resp:
        return resp
    existing = _servers_for(owner).get(name)
    if existing is None:
        return JSONResponse({"error": f"unknown server '{name}'"}, status_code=404)
    mcp_core.disconnect_one(name, owner)
    for k in existing.get("secret_env_keys") or []:
        credentials.delete_token(mcp_core.secret_env_ref(name, k, owner))
    for k in (mcp_oauth.TOKEN_KEY, mcp_oauth.DCR_KEY):
        credentials.delete_token(mcp_core.secret_env_ref(name, k, owner))
    if owner is None:
        _write_server(name, None)
    else:
        auth_db.delete_user_mcp_server(owner, name)
    audit_log(user, action="mcp.server.delete", resource=f"mcp:{name}",
              permission_key=MANAGE_PERM if owner is None else "chat.use", detail={"scope": scope})
    return {"ok": True}


@router.post("/servers/{name}/reconnect")
async def reconnect_server(name: str, scope: str = "global", user: Principal = Depends(_use)):
    owner, resp = _owner_for(scope, user)
    if resp:
        return resp
    scfg = _servers_for(owner).get(name)
    if scfg is None:
        return JSONResponse({"error": f"unknown server '{name}'"}, status_code=404)
    return {"ok": True, "status": _connect_in_background(name, scfg, owner)}


@router.post("/import")
async def import_servers(req: McpImportReq, user: Principal = Depends(_use)):
    """Paste a Claude-Desktop style {"mcpServers": {...}} block; each entry is validated like the form."""
    owner, resp = _owner_for(req.scope, user)
    if resp:
        return resp
    block = req.config.get("mcpServers", req.config)
    if not isinstance(block, dict) or not block:
        return JSONResponse({"error": 'expected {"mcpServers": {name: {...}}}'}, status_code=400)
    results = []
    for name, entry in block.items():
        if not isinstance(entry, dict):
            results.append({"name": name, "error": "entry must be an object"})
            continue
        sreq = McpServerReq(
            name=str(name).lower(), scope=req.scope,
            transport=entry.get("transport") or ("stdio" if entry.get("command") else "http"),
            command=entry.get("command"), args=entry.get("args") or [],
            url=entry.get("url") or entry.get("serverUrl"),
            env=entry.get("env") or {}, disabled=bool(entry.get("disabled")))
        err, _ = _check_new(sreq, user, owner)
        if err:
            results.append({"name": sreq.name, "error": err})
            continue
        saved = await _save_server(sreq, user, "mcp.server.import", [], owner)
        results.append({"name": sreq.name, "status": saved["status"]})
    return {"results": results}


@router.put("/user-packages")
async def set_user_packages(req: UserPackagesReq, user: Principal = Depends(_manage)):
    """Admin: packages non-admins may run as personal stdio servers via npx/uvx.
    Approve only packages that talk to a remote API - never ones that read or write this host's disk."""
    pkgs = sorted({_package_base(str(p)) for p in req.packages if str(p).strip()})
    bad = [p for p in pkgs if not _PKG_RX.match(p)]
    if bad:
        return JSONResponse({"error": f"invalid package name(s): {', '.join(bad)}"}, status_code=400)
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    old = cfg.setdefault("capabilities", {}).get("mcp_user_allowed_packages") or []
    cfg["capabilities"]["mcp_user_allowed_packages"] = pkgs
    write_app_config(cfg, CONFIG_FILE)
    APP_CONFIG.setdefault("capabilities", {})["mcp_user_allowed_packages"] = pkgs
    audit_log(user, action="mcp.user_packages.update", resource="mcp", permission_key=MANAGE_PERM,
              detail={"from": old, "to": pkgs})
    return {"ok": True, "packages": pkgs}
