from typing import Optional
from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from core.small_model import APP_CONFIG
from core import auth_db, credentials, mcp_catalog, mcp_oauth
from core import mcp as mcp_core
from core import plugins as plugins_core
from core import skills as skills_core
from core.audit import audit_log
from core.auth import Principal, user_has_permission
from core.deps import get_current_user
from routes import mcp_manager
from routes.plugins import _set_disabled as _set_plugin_disabled

from .base import InstallReq, _PERM, _err, router
from .listing import _item

_USE_PERM = "chat.use"


def _deny(user: Principal, request: Request, key: str) -> JSONResponse:
    audit_log(user, action=key, permission_key=key, result="deny",
              ip=request.client.host if request.client else None)
    return _err(f"missing permission: {key}", 403)


def _has(user: Principal, key: str) -> bool:
    return user_has_permission(user, key)


@router.post("/{kind}/{item_id}/install")
async def install(kind: str, item_id: str, request: Request, req: Optional[InstallReq] = None,
                  user: Principal = Depends(get_current_user)):
    req = req or InstallReq()
    detail: dict = {}
    perm = _PERM
    try:
        if kind == "connectors":
            res = await _install_connector(item_id, req, user, request)
            if isinstance(res, JSONResponse):
                return res
            detail = res
            perm = _USE_PERM if res["scope"] == "user" else _PERM
        else:
            if not _has(user, _PERM):
                return _deny(user, request, _PERM)
            if kind == "skills":
                if not skills_core.valid_name(item_id):
                    return _err("invalid skill name")
                skills_core.install_from_catalog(item_id)
            elif kind == "plugins":
                if not plugins_core.valid_name(item_id):
                    return _err("invalid plugin name")
                plugins_core.install_from_catalog(item_id)
                if item_id in plugins_core.disabled_names():
                    _set_plugin_disabled(item_id, False)
                if plugins_core.plugins_enabled():
                    plugins_core.load_plugin(item_id)
            else:
                return _err(f"unknown kind '{kind}'", 404)
    except ValueError as e:
        return _err(str(e))
    except OSError as e:
        return _err(f"install failed: {e}", 500)
    audit_log(user, action=f"customize.{kind}.install", resource=item_id, permission_key=perm,
              detail=detail or None)
    return {"ok": True, "item": _item(kind, item_id, user.id)}


async def _install_connector(item_id: str, req: InstallReq, user: Principal, request: Request):
    pre = mcp_catalog.get_preset(item_id)
    if not pre:
        return _err(f"'{item_id}' is not in the connector catalog", 404)
    scope, err = mcp_catalog.effective_scope(pre, req.scope, _has(user, _PERM))
    if err:
        return _err(err, 403)
    need = _PERM if scope == "global" else _USE_PERM
    if not _has(user, need):
        return _deny(user, request, need)
    if not mcp_catalog.connector_allowed(pre) and not _has(user, _PERM):
        return _err(f"'{pre['name']}' handles sensitive data and has not been enabled for users yet. "
                    "Ask an administrator to allow it.", 403)
    owner = None if scope == "global" else user.id
    if item_id in mcp_manager._servers_for(owner):
        return _err(f"connector '{item_id}' is already installed", 409)
    if owner is not None and item_id in mcp_core.configured_servers(APP_CONFIG):
        return _err(f"'{item_id}' is already installed for everyone", 409)
    wanted = [s["key"] for s in pre.get("secret_env", [])]
    missing = [k for k in wanted if not str(req.secrets.get(k) or "").strip()]
    if missing:
        return _err(f"missing required value(s): {', '.join(missing)}")
    url = pre.get("url")
    if pre.get("fields"):
        url, ferr = mcp_catalog.resolve_url(pre, req.fields)
        if ferr:
            return _err(ferr)
    sreq = mcp_manager.McpServerReq(
        name=item_id, scope=scope, transport=pre["transport"], command=pre.get("command"),
        args=list(pre.get("args", [])), url=url, auth=dict(pre["auth"]) if pre.get("auth") else None,
        # only the preset's declared keys are accepted - no arbitrary env injection
        secret_env={k: str(req.secrets[k]).strip() for k in wanted})
    err = mcp_manager._validate(sreq, mcp_manager._restricted(user, owner), preset=pre)
    if err:
        return _err(err)
    # _save_server stores secrets in the keychain, writes config (global) or the user's row, audits and connects
    saved = await mcp_manager._save_server(sreq, user, "mcp.server.add", [], owner)
    return {"command": sreq.command, "url": sreq.url, "scope": scope, "secret_env_keys": wanted,
            "status": saved["status"]["status"]}


def remove_connector(item_id: str, owner: Optional[int]) -> None:
    """Disconnect a connector and delete everything it stored: config or user row, env secrets, and the
    OAuth token, registered client and client secret that belong to this owner."""
    scfg = mcp_manager._servers_for(owner).get(item_id) or {}
    mcp_core.disconnect_one(item_id, owner)
    keys = set(scfg.get("secret_env_keys") or []) | {mcp_oauth.TOKEN_KEY, mcp_oauth.DCR_KEY, mcp_oauth.SECRET_KEY}
    for k in keys:
        credentials.delete_token(mcp_core.secret_env_ref(item_id, k, owner))
    mcp_oauth.set_status(item_id, owner, "idle")
    if owner is None:
        mcp_manager._write_server(item_id, None)
    else:
        auth_db.delete_user_mcp_server(owner, item_id)


@router.delete("/{kind}/{item_id}")
async def uninstall(kind: str, item_id: str, request: Request, user: Principal = Depends(get_current_user)):
    perm = _PERM
    try:
        if kind == "connectors":
            if not mcp_catalog.get_preset(item_id):
                return _err("custom servers are managed in Settings → Capabilities → MCP")
            # the caller's own install first; removing the shared one needs capabilities.install
            if item_id in mcp_core.user_servers(user.id):
                owner, perm = user.id, _USE_PERM
            elif item_id in mcp_core.configured_servers(APP_CONFIG):
                owner = None
            else:
                return _err(f"'{item_id}' is not installed")
            if not _has(user, perm):
                return _deny(user, request, perm)
            remove_connector(item_id, owner)
        else:
            if not _has(user, _PERM):
                return _deny(user, request, _PERM)
            if kind == "skills":
                if not skills_core.valid_name(item_id):
                    return _err("invalid skill name")
                skills_core.uninstall(item_id)
            elif kind == "plugins":
                if not plugins_core.valid_name(item_id):
                    return _err("invalid plugin name")
                plugins_core.uninstall(item_id)
                if item_id in plugins_core.disabled_names():
                    _set_plugin_disabled(item_id, False)
            else:
                return _err(f"unknown kind '{kind}'", 404)
    except ValueError as e:
        return _err(str(e))
    except OSError as e:
        return _err(f"uninstall failed: {e}", 500)
    audit_log(user, action=f"customize.{kind}.uninstall", resource=item_id, permission_key=perm)
    return {"ok": True}
