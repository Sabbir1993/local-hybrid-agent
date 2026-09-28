"""
routes/mcp_manager.py - MCP connector catalog: list known connectors, authorize one via
OAuth device flow, disconnect one. Tokens are stored in the OS keychain (core/credentials.py)
and never written to config/app.json - only transport/command/args + a "credential_ref"
marker are persisted there. See core/mcp_catalog.py for the connector list.
"""

import asyncio
import ipaddress
import json
import re
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from core.config import BASE_DIR, CONFIG_FILE, write_app_config
from core.net_guard import BlockedURLError, check_url
from core.small_model import APP_CONFIG
from core import credentials, mcp_catalog, mcp_oauth
from core import mcp as mcp_core
from core import auth_db
from core.audit import audit_log
from core.auth import Principal, user_has_permission
from core.deps import require_permission

# global MCP servers and their OAuth tokens are shared by every user of this instance
MANAGE_PERM = "settings.orchestration.configure"
_manage = require_permission(MANAGE_PERM)
# a personal server ("just for me") only needs the ordinary app permission
_use = require_permission("chat.use")

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


# ---------------- custom servers (Settings -> Capabilities -> MCP) ----------------
# scope "global": runs for every user, so admin permission, a command allowlist, https-only
# remote URLs, secrets in the OS keychain, and an audit row.
# scope "user": a personal server only its owner sees (auth.db user_mcp_servers, secrets under
# the owner's keychain ids). Non-admins get a narrower check (_validate restricted=True) since
# a stdio server still runs on this host: public https URL, or npx/uvx with an admin-approved package.

_NAME_RX = re.compile(r"^[a-z0-9_-]{1,32}$")
_ENV_KEY_RX = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_PKG_RX = re.compile(r"^(@[a-z0-9._-]+/)?[a-z0-9._-]{1,100}$")
DEFAULT_ALLOWED_COMMANDS = ["npx", "node", "python", "uvx"]
# node/python are only accepted to run a script file shipped inside this repo
# (e.g. core/echo_mcp_server.py) -- never -c / -e / -m or a path outside BASE_DIR.
_SCRIPT_COMMANDS = ("node", "python")
_USER_COMMANDS = ("npx", "uvx")
# env vars that change how npx/uvx/node/python resolve or load code - a non-admin could
# otherwise point the approved package at another registry or preload their own script
_USER_ENV_DENY_PREFIXES = ("NODE_", "NPM_", "NPX_", "UV_", "PIP_", "PYTHON", "LD_", "DYLD_", "SSL_")
_USER_ENV_DENY = {"PATH", "PATHEXT", "COMSPEC", "SYSTEMROOT", "WINDIR", "HOME", "USERPROFILE", "APPDATA",
                  "LOCALAPPDATA", "TEMP", "TMP", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                  "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"}


class McpServerReq(BaseModel):
    name: str
    scope: str = "global"             # global | user
    from_scope: Optional[str] = None  # original scope if changing availability on edit
    transport: str = "stdio"          # stdio | http
    command: Optional[str] = None
    args: list = []
    url: Optional[str] = None
    env: dict = {}                    # plain values, stored in config/app.json (global) / auth.db (user)
    secret_env: dict = {}             # values -> OS keychain; blank value on edit = keep existing
    headers: dict = {}                # http only: extra non-secret request headers
    auth: Optional[dict] = None       # http only: {"type": "oauth", client_id, scopes, auth_url?, token_url?, ...}
    disabled: bool = False


class McpImportReq(BaseModel):
    config: dict                      # {"mcpServers": {...}} or the inner mapping
    scope: str = "global"


class UserPackagesReq(BaseModel):
    packages: list


def _allowed_commands() -> list:
    return APP_CONFIG.get("capabilities", {}).get("mcp_allowed_commands") or DEFAULT_ALLOWED_COMMANDS


def _user_packages() -> list:
    """Packages a non-admin may run as a personal stdio server (capabilities.mcp_user_allowed_packages)."""
    return [str(p).strip().lower() for p in
            (APP_CONFIG.get("capabilities", {}).get("mcp_user_allowed_packages") or []) if str(p).strip()]


# name, optional version: '@scope/pkg', 'pkg@1.2.3', 'pkg@latest', 'pkg==1.0', 'pkg[extra]'.
# No aliases (npm:), URLs, git+/file: specs, paths, or whitespace -- all of those make npx/uvx
# install something other than the approved name.
_SPEC_RX = re.compile(
    r"^(@[a-z0-9._-]+/)?[a-z0-9._-]{1,100}(\[[a-z0-9,_-]+\])?"
    r"((@|==)[a-z0-9.+^~*-]{1,64})?$")


def _package_base(spec: str) -> str:
    """'@scope/pkg@1.2' / 'pkg@latest' / 'pkg==1.0' / 'pkg[extra]' -> the bare package name."""
    spec = spec.strip().lower()
    if spec.startswith("@"):
        at = spec.find("@", 1)
        return spec if at == -1 else spec[:at]
    return re.split(r"[@=<>!~\[ ]", spec, maxsplit=1)[0]


def _validate_user_stdio(req: McpServerReq) -> Optional[str]:
    cmd = (req.command or "").strip()
    if cmd not in _USER_COMMANDS:
        return f"personal stdio servers must use {' or '.join(_USER_COMMANDS)} with an approved package"
    args = [str(a) for a in req.args if str(a) != ""]
    i = 0
    # only npx's -y/--yes may precede the package: --package, --from, --with, --index ...
    # would pull in something other than the approved package
    while i < len(args) and cmd == "npx" and args[i] in ("-y", "--yes"):
        i += 1
    if i >= len(args) or args[i].startswith("-"):
        return f"the first {cmd} argument must be the package name (only -y may come before it for npx)"
    if not _SPEC_RX.match(args[i].strip().lower()):
        return f"'{args[i]}' is not a plain package spec (use name or name@version)"
    pkg = _package_base(args[i])
    allowed = _user_packages()
    if pkg not in allowed:
        return (f"package '{pkg}' is not approved for personal servers; approved: "
                f"{', '.join(allowed) or '(none - ask an admin)'}")
    return None


def _validate_script_command(cmd: str, args: list) -> Optional[str]:
    args = [str(a) for a in args if str(a) != ""]
    if not args or args[0].startswith("-"):
        return f"'{cmd}' may only run a script file from this app's folder (no -c/-e/-m)"
    script = (BASE_DIR / args[0]).resolve()
    if not script.is_file() or not script.is_relative_to(BASE_DIR):
        return f"'{cmd}' script must be an existing file inside {BASE_DIR}"
    return None


def _public_url_error(url: str) -> Optional[str]:
    u = urlparse(url or "")
    if not (u.scheme == "https" and u.hostname) or _is_internal_host(u.hostname):
        return "url must be a public https:// address"
    try:
        check_url(url)   # resolves DNS: 127.0.0.1.nip.io and friends are caught here
    except BlockedURLError as e:
        return f"url must be a public https:// address ({e})"
    return None


def _is_internal_host(host: str) -> bool:
    """localhost and loopback / private / link-local IP literals - a personal server must not
    make this host call its own internal services."""
    h = (host or "").strip("[]").lower()
    if h == "localhost" or h.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified


def _validate(req: McpServerReq, restricted: bool = False) -> Optional[str]:
    if not _NAME_RX.match(req.name or ""):
        return "name must be 1-32 chars of a-z, 0-9, _ or -"
    if req.scope not in ("global", "user"):
        return "scope must be 'global' or 'user'"
    if req.transport not in ("stdio", "http"):
        return "transport must be 'stdio' or 'http'"
    if req.transport == "stdio":
        if restricted:
            err = _validate_user_stdio(req)
            if err:
                return err
        else:
            cmd = (req.command or "").strip()
            if cmd not in _allowed_commands():
                return (f"command '{cmd}' is not allowed; allowed: {', '.join(_allowed_commands())} "
                        f"(capabilities.mcp_allowed_commands in config/app.json)")
            if cmd in _SCRIPT_COMMANDS:
                err = _validate_script_command(cmd, req.args)
                if err:
                    return err
    else:
        u = urlparse(req.url or "")
        if restricted:
            err = _public_url_error(req.url)
            if err:
                return err
        else:
            local = u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost")
            if not (u.scheme == "https" and u.hostname) and not local:
                return "url must be https:// (or http://127.0.0.1 / http://localhost)"
    if req.transport == "http":
        err = _validate_auth(req, restricted)
        if err:
            return err
    elif req.auth or req.headers:
        return "auth/headers only apply to http servers"
    for k in list(req.env) + list(req.secret_env):
        if not _ENV_KEY_RX.match(str(k)):
            return f"invalid env var name '{k}'"
        ku = str(k).upper()
        if restricted and (ku in _USER_ENV_DENY or ku.startswith(_USER_ENV_DENY_PREFIXES)):
            return f"env var '{k}' is not allowed on a personal server"
    return None


_HEADER_RX = re.compile(r"^[A-Za-z0-9-]{1,64}$")
# secrets belong in secret_env (keychain), never in plain headers stored in the config
_SECRET_HEADERS = {"authorization", "proxy-authorization", "cookie", "x-api-key", "api-key"}


def _validate_auth(req: McpServerReq, restricted: bool) -> Optional[str]:
    for k, v in (req.headers or {}).items():
        if not _HEADER_RX.match(str(k)):
            return f"invalid header name '{k}'"
        if str(k).lower() in _SECRET_HEADERS or str(k).lower().startswith("mcp-"):
            return f"header '{k}' can't be set here (put secrets in secret env, e.g. AUTHORIZATION)"
        if len(str(v)) > 512:
            return f"header '{k}' is too long"
    a = req.auth
    if not a:
        return None
    if not isinstance(a, dict) or a.get("type") != "oauth":
        return "auth.type must be 'oauth'"
    if "client_secret" in a:
        return "client_secret goes in secret env as OAUTH_CLIENT_SECRET"
    unknown = set(a) - {"type", "client_id", "scopes", "auth_url", "token_url", "redirect_port",
                        "extra_params", "resource_param"}
    if unknown:
        return f"unknown auth field(s): {', '.join(sorted(unknown))}"
    for k in ("auth_url", "token_url"):
        if a.get(k):
            err = _public_url_error(str(a[k])) if restricted else (
                None if urlparse(str(a[k])).scheme == "https" else f"auth.{k} must be https://")
            if err:
                return f"auth.{k}: {err}"
    port = a.get("redirect_port") or 0
    if not isinstance(port, int) or not (port == 0 or 1024 <= port <= 65535):
        return "auth.redirect_port must be 0 or 1024-65535"
    scopes = a.get("scopes") or []
    if not isinstance(scopes, list) or not all(isinstance(x, str) and len(x) < 200 for x in scopes):
        return "auth.scopes must be a list of strings"
    if not isinstance(a.get("extra_params") or {}, dict):
        return "auth.extra_params must be an object"
    return None


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


# ---------------- OAuth sign-in for http servers (core/mcp_oauth.py) ----------------

def _redirect_base() -> str:
    """Public https URL of this app, if the admin set one: enables the server-side callback."""
    return str(APP_CONFIG.get("capabilities", {}).get("mcp_oauth_redirect_base") or "").rstrip("/")


class OAuthStartReq(BaseModel):
    scope: str = "user"


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
