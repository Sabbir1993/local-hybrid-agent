import asyncio
import json
from typing import Optional
from fastapi.responses import JSONResponse
from core.config import CONFIG_FILE, write_app_config
from core.small_model import APP_CONFIG
from core import credentials, mcp_oauth
from core import mcp as mcp_core
from core import auth_db
from core.audit import audit_log
from core.auth import Principal, user_has_permission

from .base import MANAGE_PERM
from .models import McpServerReq
from .validation import _validate


def _server_cfg(entry: dict) -> dict:
    return {
        "transport": entry["transport"],
        "command": entry["command"],
        "args": entry.get("args", []),
        "credential_ref": "keyring",
    }


def _persist_server_entry(server_id: str, entry: dict) -> None:
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    servers = cfg.setdefault("capabilities", {}).setdefault("mcp_servers", {})
    servers[server_id] = _server_cfg(entry)
    write_app_config(cfg, CONFIG_FILE)
    APP_CONFIG.setdefault("capabilities", {}).setdefault("mcp_servers", {})[server_id] = servers[server_id]


def _remove_server_entry(server_id: str) -> None:
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    servers = cfg.setdefault("capabilities", {}).setdefault("mcp_servers", {})
    servers.pop(server_id, None)
    write_app_config(cfg, CONFIG_FILE)
    (APP_CONFIG.get("capabilities", {}).get("mcp_servers", {}) or {}).pop(server_id, None)


def _can_manage_global(user: Principal) -> bool:
    return user_has_permission(user, MANAGE_PERM)


def _owner_for(scope: str, user: Principal):
    """(owner, error response). owner None = global (needs MANAGE_PERM), else the caller's id."""
    if scope == "user":
        return user.id, None
    if scope != "global":
        return None, JSONResponse({"error": "scope must be 'global' or 'user'"}, status_code=400)
    if not _can_manage_global(user):
        audit_log(user, action=MANAGE_PERM, permission_key=MANAGE_PERM, result="deny",
                  detail={"via": "mcp.server", "scope": "global"})
        return None, JSONResponse({"error": f"missing permission: {MANAGE_PERM} "
                                            "(add it as a personal server instead)"}, status_code=403)
    return None, None


def _restricted(user: Principal, owner: Optional[int]) -> bool:
    return owner is not None and not _can_manage_global(user)


def _servers_for(owner: Optional[int]) -> dict:
    return mcp_core.configured_servers(APP_CONFIG) if owner is None else mcp_core.user_servers(owner)


def _custom_cfg(req: McpServerReq, keep_secret_keys: list) -> dict:
    cfg: dict = {"transport": req.transport}
    if req.transport == "stdio":
        cfg["command"] = req.command.strip()
        cfg["args"] = [str(a) for a in req.args if str(a) != ""]
    else:
        cfg["url"] = req.url.strip()
        if req.headers:
            cfg["headers"] = {str(k): str(v) for k, v in req.headers.items()}
        if req.auth:
            a = {k: v for k, v in req.auth.items() if v not in (None, "", [], {})}
            a["type"] = "oauth"
            cfg["auth"] = a
    if req.env:
        cfg["env"] = {str(k): str(v) for k, v in req.env.items()}
    secret_keys = sorted(set(keep_secret_keys) | {k for k, v in req.secret_env.items() if str(v)})
    if secret_keys:
        cfg["secret_env_keys"] = secret_keys
    if req.disabled:
        cfg["disabled"] = True
    return cfg


def _write_server(name: str, scfg: Optional[dict]) -> None:
    """Set (or remove when scfg is None) one entry in capabilities.mcp_servers, file + live."""
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    servers = cfg.setdefault("capabilities", {}).setdefault("mcp_servers", {})
    live = APP_CONFIG.setdefault("capabilities", {}).setdefault("mcp_servers", {})
    if scfg is None:
        servers.pop(name, None)
        live.pop(name, None)
        # an entry that came from a pasted top-level mcpServers block goes too
        for block in (cfg.get("mcpServers"), APP_CONFIG.get("mcpServers")):
            if isinstance(block, dict):
                block.pop(name, None)
    else:
        servers[name] = scfg
        live[name] = scfg
    write_app_config(cfg, CONFIG_FILE)


def _public_cfg(name: str, scfg: dict, scope: str = "global", owner: Optional[int] = None) -> dict:
    """Config as shown to the UI - secret values never leave the keychain."""
    return {
        "name": name,
        "scope": scope,
        "transport": scfg.get("transport") or ("stdio" if scfg.get("command") else "http"),
        "command": scfg.get("command"),
        "args": scfg.get("args") or [],
        "url": scfg.get("url"),
        "env": scfg.get("env") or {},
        "secret_env_keys": scfg.get("secret_env_keys") or [],
        "headers": scfg.get("headers") or {},
        "auth": scfg.get("auth") or None,
        "oauth_signed_in": bool(scfg.get("auth")) and mcp_oauth.has_token(name, None if scope == "global" else owner),
        "disabled": bool(scfg.get("disabled")),
        "managed": scfg.get("credential_ref") == "keyring",   # catalog connector: use Connect/Disconnect
    }


async def _save_server(req: McpServerReq, user: Principal, action: str, keep_secret_keys: list,
                       owner: Optional[int] = None) -> dict:
    for k, v in req.secret_env.items():
        if str(v):
            credentials.set_token(mcp_core.secret_env_ref(req.name, k, owner), str(v))
    scfg = _custom_cfg(req, keep_secret_keys)
    if owner is None:
        _write_server(req.name, scfg)
    else:
        auth_db.upsert_user_mcp_server(owner, req.name, scfg)
    scope = "global" if owner is None else "user"
    audit_log(user, action=action, resource=f"mcp:{req.name}",
              permission_key=MANAGE_PERM if owner is None else "chat.use",
              detail={"scope": scope, "transport": scfg["transport"], "command": scfg.get("command"),
                      "args": scfg.get("args"), "url": scfg.get("url"),
                      "secret_env_keys": scfg.get("secret_env_keys", [])})
    return {"ok": True, "server": _public_cfg(req.name, scfg, scope, owner),
            "status": _connect_in_background(req.name, scfg, owner)}


_bg_tasks: set = set()


def _connect_in_background(name: str, scfg: dict, owner: Optional[int] = None) -> dict:
    """npx/mcp-remote can take a minute+ (package download, OAuth in a browser) - don't hold
    the request open; the UI polls /control/capabilities until the server leaves 'connecting'."""
    task = asyncio.create_task(mcp_core.connect_one(name, scfg, owner))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return {"name": name, "status": "disabled" if scfg.get("disabled") else "connecting"}


def _check_new(req: McpServerReq, user: Principal, owner: Optional[int]):
    """(error, http status) for a server about to be added, or (None, 200)."""
    err = _validate(req, _restricted(user, owner))
    if err:
        return err, 400
    if req.name in _servers_for(owner):
        return f"server '{req.name}' already exists", 409
    if owner is not None and req.name in mcp_core.configured_servers(APP_CONFIG):
        return f"'{req.name}' is already a global server - pick another name", 409
    return None, 200
