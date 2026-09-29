from typing import Optional
from fastapi import Depends
from fastapi.responses import JSONResponse
from core.small_model import APP_CONFIG
from core import credentials, mcp_catalog
from core import mcp as mcp_core
from core import plugins as plugins_core
from core import skills as skills_core
from core.audit import audit_log
from core.auth import Principal
from routes import mcp_manager
from routes.plugins import _set_disabled as _set_plugin_disabled

from .base import InstallReq, _PERM, _err, _manage, router
from .listing import _item


@router.post("/{kind}/{item_id}/install")
async def install(kind: str, item_id: str, req: Optional[InstallReq] = None,
                  user: Principal = Depends(_manage)):
    req = req or InstallReq()
    detail: dict = {}
    try:
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
        elif kind == "connectors":
            res = await _install_connector(item_id, req, user)
            if isinstance(res, JSONResponse):
                return res
            detail = res
        else:
            return _err(f"unknown kind '{kind}'", 404)
    except ValueError as e:
        return _err(str(e))
    except OSError as e:
        return _err(f"install failed: {e}", 500)
    audit_log(user, action=f"customize.{kind}.install", resource=item_id, permission_key=_PERM,
              detail=detail or None)
    return {"ok": True, "item": _item(kind, item_id)}


async def _install_connector(item_id: str, req: InstallReq, user: Principal):
    pre = mcp_catalog.get_preset(item_id)
    if not pre:
        return _err(f"'{item_id}' is not in the connector catalog", 404)
    if item_id in mcp_core.configured_servers(APP_CONFIG):
        return _err(f"connector '{item_id}' is already installed", 409)
    wanted = [s["key"] for s in pre.get("secret_env", [])]
    missing = [k for k in wanted if not str(req.secrets.get(k) or "").strip()]
    if missing:
        return _err(f"missing required value(s): {', '.join(missing)}")
    sreq = mcp_manager.McpServerReq(
        name=item_id, transport=pre["transport"], command=pre.get("command"),
        args=list(pre.get("args", [])), url=pre.get("url"),
        # only the preset's declared keys are accepted - no arbitrary env injection
        secret_env={k: str(req.secrets[k]).strip() for k in wanted})
    err = mcp_manager._validate(sreq)
    if err:
        return _err(err)
    # _save_server stores secrets in the keychain, writes app.json, audits and connects
    saved = await mcp_manager._save_server(sreq, user, "mcp.server.add", [])
    return {"command": sreq.command, "secret_env_keys": wanted, "status": saved["status"]["status"]}


@router.delete("/{kind}/{item_id}")
async def uninstall(kind: str, item_id: str, user: Principal = Depends(_manage)):
    try:
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
        elif kind == "connectors":
            if not mcp_catalog.get_preset(item_id):
                return _err("custom servers are managed in Settings → Capabilities → MCP")
            scfg = mcp_core.configured_servers(APP_CONFIG).get(item_id)
            if scfg is None:
                return _err(f"'{item_id}' is not installed")
            mcp_core.disconnect_one(item_id)
            for k in scfg.get("secret_env_keys") or []:
                credentials.delete_token(mcp_core.secret_env_ref(item_id, k))
            mcp_manager._write_server(item_id, None)
        else:
            return _err(f"unknown kind '{kind}'", 404)
    except ValueError as e:
        return _err(str(e))
    except OSError as e:
        return _err(f"uninstall failed: {e}", 500)
    audit_log(user, action=f"customize.{kind}.uninstall", resource=item_id, permission_key=_PERM)
    return {"ok": True}
