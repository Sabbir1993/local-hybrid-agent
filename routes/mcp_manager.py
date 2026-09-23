"""
routes/mcp_manager.py - MCP connector catalog: list known connectors, authorize one via
OAuth device flow, disconnect one. Tokens are stored in the OS keychain (core/credentials.py)
and never written to config/app.json - only transport/command/args + a "credential_ref"
marker are persisted there. See core/mcp_catalog.py for the connector list.
"""

import asyncio
import json
import re
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.config import CONFIG_FILE
from core.small_model import APP_CONFIG
from core import credentials, mcp_catalog
from core import mcp as mcp_core
from core.audit import audit_log
from core.auth import Principal
from core.deps import require_permission

# MCP servers and their OAuth tokens are shared by every user of this instance
_manage = require_permission("settings.orchestration.configure")

router = APIRouter(prefix="/mcp", tags=["mcp"])


def _server_status(server_id: str) -> Optional[dict]:
    for s in mcp_core.mcp_status():
        if s["name"] == server_id:
            return s
    return None


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
    return {"ok": True}


# ---------------- config/app.json persistence (secret-free) ----------------

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
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    APP_CONFIG.setdefault("capabilities", {}).setdefault("mcp_servers", {})[server_id] = servers[server_id]


def _remove_server_entry(server_id: str) -> None:
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    servers = cfg.setdefault("capabilities", {}).setdefault("mcp_servers", {})
    servers.pop(server_id, None)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    (APP_CONFIG.get("capabilities", {}).get("mcp_servers", {}) or {}).pop(server_id, None)


# ---------------- custom servers (Settings -> Capabilities -> MCP) ----------------
# Adding a stdio server runs a command on this host for every user, so: admin permission,
# a command allowlist, https-only remote URLs, secrets in the OS keychain, and an audit row.

_NAME_RX = re.compile(r"^[a-z0-9_-]{1,32}$")
_ENV_KEY_RX = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
DEFAULT_ALLOWED_COMMANDS = ["npx", "node", "python", "uvx"]


class McpServerReq(BaseModel):
    name: str
    transport: str = "stdio"          # stdio | http
    command: Optional[str] = None
    args: list = []
    url: Optional[str] = None
    env: dict = {}                    # plain values, stored in config/app.json
    secret_env: dict = {}             # values -> OS keychain; blank value on edit = keep existing
    disabled: bool = False


class McpImportReq(BaseModel):
    config: dict                      # {"mcpServers": {...}} or the inner mapping


def _allowed_commands() -> list:
    return APP_CONFIG.get("capabilities", {}).get("mcp_allowed_commands") or DEFAULT_ALLOWED_COMMANDS


def _validate(req: McpServerReq) -> Optional[str]:
    if not _NAME_RX.match(req.name or ""):
        return "name must be 1-32 chars of a-z, 0-9, _ or -"
    if req.transport not in ("stdio", "http"):
        return "transport must be 'stdio' or 'http'"
    if req.transport == "stdio":
        cmd = (req.command or "").strip()
        if cmd not in _allowed_commands():
            return (f"command '{cmd}' is not allowed; allowed: {', '.join(_allowed_commands())} "
                    f"(capabilities.mcp_allowed_commands in config/app.json)")
    else:
        u = urlparse(req.url or "")
        local = u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost")
        if not (u.scheme == "https" and u.hostname) and not local:
            return "url must be https:// (or http://127.0.0.1 / http://localhost)"
    for k in list(req.env) + list(req.secret_env):
        if not _ENV_KEY_RX.match(str(k)):
            return f"invalid env var name '{k}'"
    return None


def _custom_cfg(req: McpServerReq, keep_secret_keys: list) -> dict:
    cfg: dict = {"transport": req.transport}
    if req.transport == "stdio":
        cfg["command"] = req.command.strip()
        cfg["args"] = [str(a) for a in req.args if str(a) != ""]
    else:
        cfg["url"] = req.url.strip()
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
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def _public_cfg(name: str, scfg: dict) -> dict:
    """Config as shown to the UI - secret values never leave the keychain."""
    return {
        "name": name,
        "transport": scfg.get("transport") or ("stdio" if scfg.get("command") else "http"),
        "command": scfg.get("command"),
        "args": scfg.get("args") or [],
        "url": scfg.get("url"),
        "env": scfg.get("env") or {},
        "secret_env_keys": scfg.get("secret_env_keys") or [],
        "disabled": bool(scfg.get("disabled")),
        "managed": scfg.get("credential_ref") == "keyring",   # catalog connector: use Connect/Disconnect
    }


async def _save_server(req: McpServerReq, user: Principal, action: str, keep_secret_keys: list) -> dict:
    for k, v in req.secret_env.items():
        if str(v):
            credentials.set_token(mcp_core.secret_env_ref(req.name, k), str(v))
    scfg = _custom_cfg(req, keep_secret_keys)
    _write_server(req.name, scfg)
    audit_log(user, action=action, resource=f"mcp:{req.name}",
              permission_key="settings.orchestration.configure",
              detail={"transport": scfg["transport"], "command": scfg.get("command"),
                      "url": scfg.get("url"), "secret_env_keys": scfg.get("secret_env_keys", [])})
    return {"ok": True, "server": _public_cfg(req.name, scfg), "status": _connect_in_background(req.name, scfg)}


_bg_tasks: set = set()


def _connect_in_background(name: str, scfg: dict) -> dict:
    """npx/mcp-remote can take a minute+ (package download, OAuth in a browser) - don't hold
    the request open; the UI polls /control/capabilities until the server leaves 'connecting'."""
    task = asyncio.create_task(mcp_core.connect_one(name, scfg))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return {"name": name, "status": "disabled" if scfg.get("disabled") else "connecting"}


@router.get("/servers")
async def list_servers(user: Principal = Depends(_manage)):
    return {
        "servers": [_public_cfg(n, s) for n, s in mcp_core.configured_servers(APP_CONFIG).items()],
        "allowed_commands": _allowed_commands(),
    }


@router.post("/servers")
async def add_server(req: McpServerReq, user: Principal = Depends(_manage)):
    err = _validate(req)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    if req.name in mcp_core.configured_servers(APP_CONFIG):
        return JSONResponse({"error": f"server '{req.name}' already exists"}, status_code=409)
    return await _save_server(req, user, "mcp.server.add", [])


@router.put("/servers/{name}")
async def update_server(name: str, req: McpServerReq, user: Principal = Depends(_manage)):
    req.name = name
    err = _validate(req)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    existing = mcp_core.configured_servers(APP_CONFIG).get(name)
    if existing is None:
        return JSONResponse({"error": f"unknown server '{name}'"}, status_code=404)
    # a secret key left out of the request is dropped; one sent blank keeps its stored value
    old_keys = existing.get("secret_env_keys") or []
    for k in old_keys:
        if k not in req.secret_env:
            credentials.delete_token(mcp_core.secret_env_ref(name, k))
    keep = [k for k in old_keys if k in req.secret_env and not str(req.secret_env[k])]
    return await _save_server(req, user, "mcp.server.update", keep)


@router.delete("/servers/{name}")
async def delete_server(name: str, user: Principal = Depends(_manage)):
    existing = mcp_core.configured_servers(APP_CONFIG).get(name)
    if existing is None:
        return JSONResponse({"error": f"unknown server '{name}'"}, status_code=404)
    mcp_core.disconnect_one(name)
    for k in existing.get("secret_env_keys") or []:
        credentials.delete_token(mcp_core.secret_env_ref(name, k))
    _write_server(name, None)
    audit_log(user, action="mcp.server.delete", resource=f"mcp:{name}",
              permission_key="settings.orchestration.configure")
    return {"ok": True}


@router.post("/servers/{name}/reconnect")
async def reconnect_server(name: str, user: Principal = Depends(_manage)):
    scfg = mcp_core.configured_servers(APP_CONFIG).get(name)
    if scfg is None:
        return JSONResponse({"error": f"unknown server '{name}'"}, status_code=404)
    return {"ok": True, "status": _connect_in_background(name, scfg)}


@router.post("/import")
async def import_servers(req: McpImportReq, user: Principal = Depends(_manage)):
    """Paste a Claude-Desktop style {"mcpServers": {...}} block; each entry is validated like the form."""
    block = req.config.get("mcpServers", req.config)
    if not isinstance(block, dict) or not block:
        return JSONResponse({"error": 'expected {"mcpServers": {name: {...}}}'}, status_code=400)
    existing = mcp_core.configured_servers(APP_CONFIG)
    results = []
    for name, entry in block.items():
        if not isinstance(entry, dict):
            results.append({"name": name, "error": "entry must be an object"})
            continue
        sreq = McpServerReq(
            name=str(name).lower(),
            transport=entry.get("transport") or ("stdio" if entry.get("command") else "http"),
            command=entry.get("command"), args=entry.get("args") or [],
            url=entry.get("url") or entry.get("serverUrl"),
            env=entry.get("env") or {}, disabled=bool(entry.get("disabled")))
        err = _validate(sreq)
        if not err and sreq.name in existing:
            err = f"server '{sreq.name}' already exists"
        if err:
            results.append({"name": sreq.name, "error": err})
            continue
        saved = await _save_server(sreq, user, "mcp.server.import", [])
        results.append({"name": sreq.name, "status": saved["status"]})
    return {"results": results}
