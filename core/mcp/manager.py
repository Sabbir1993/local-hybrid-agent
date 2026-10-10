import asyncio
import json
import sys
from typing import Optional

from ..injection_guard import mcp_tool_is_read_only
from ..registry import registry
from .constants import configured_servers, server_key
from .server import McpServer

_servers: dict[str, McpServer] = {}


def _tool_bridge(server: McpServer, tool_name: str):
    async def _bridge(args: dict) -> str:
        return await server.call_tool(tool_name, args or {})
    return _bridge


def _bridge_schema(server_name: str, tool: dict) -> dict:
    """Convert an MCP tool def into an OpenAI function schema."""
    name = f"mcp__{server_name}__{tool.get('name', '?')}"
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": (f"[mcp:{server_name}] " + (tool.get("description") or tool.get("name", "")))[:400],
            "parameters": tool.get("inputSchema") or {"type": "object", "properties": {}},
        },
    }


async def connect_all_mcp() -> dict:
    """Connect every configured server and register its tools."""
    from ..small_model import APP_CONFIG
    cfg = APP_CONFIG.get("capabilities", {})
    if not cfg.get("mcp", False):
        return {}
    from .. import auth_db
    targets = [(n, s, None) for n, s in configured_servers(APP_CONFIG).items()]
    targets += [(n, s, uid) for uid, n, s in auth_db.list_user_mcp_servers()]
    targets = [(n, _migrate_plain_oauth(n, s, uid), uid) for n, s, uid in targets]
    infos = await asyncio.gather(*(connect_one(n, s, uid) for n, s, uid in targets))
    return {server_key(n, uid): info for (n, _, uid), info in zip(targets, infos)}


def _migrate_plain_oauth(name: str, cfg: dict, owner: Optional[int]) -> dict:
    """Move a plaintext clientId/clientSecret env pair into an oauth auth block + keychain."""
    from .. import mcp_oauth
    try:
        new = mcp_oauth.migrate_plain_client(name, owner, cfg)
        if new is None:
            return cfg
        if owner is None:
            from ..config import CONFIG_FILE, write_app_config
            from ..small_model import APP_CONFIG
            disk = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            servers = disk.setdefault("capabilities", {}).setdefault("mcp_servers", {})
            if name not in servers:
                return cfg
            servers[name] = new
            write_app_config(disk, CONFIG_FILE)
            APP_CONFIG.setdefault("capabilities", {}).setdefault("mcp_servers", {})[name] = new
        else:
            from .. import auth_db
            auth_db.upsert_user_mcp_server(owner, name, new)
        print(f"[mcp] '{name}': moved plaintext OAuth client credentials into the keychain")
        return new
    except Exception as e:
        print(f"[mcp] '{name}': oauth credential migration failed: {type(e).__name__}", file=sys.stderr)
        return cfg


def ready_tool_schemas() -> list:
    """Schemas of every tool on a connected (ready) MCP server - for Chat mode's tool list."""
    out = []
    for t in registry.list():
        srv = _servers.get(t.source[4:]) if t.source.startswith("mcp:") else None
        if srv and srv.status == "ready":
            out.append(t.schema)
    return out


def _visible_servers() -> list:
    """Global servers plus the calling user's personal ones (a global name shadows a personal one)."""
    from ..request_context import get_current_user_id
    uid = get_current_user_id()
    glob = {s.name for s in _servers.values() if s.owner is None}
    return [s for s in _servers.values()
            if s.owner is None or (s.owner == uid and uid is not None and s.name not in glob)]


def chat_prompt(only_servers: Optional[set] = None) -> str:
    lines = []
    for s in _visible_servers():
        if s.status != "ready" or not s.tools:
            continue
        if only_servers is not None and s.name not in only_servers:
            continue
        descs = "; ".join((t.get("description") or t.get("name", ""))[:110] for t in s.tools[:3])
        names = ", ".join(f"`mcp__{s.name}__{t.get('name')}`" for t in s.tools[:12])
        lines.append(f"- `{s.name}`: {descs}\n  tools: {names}")
    if not lines:
        return ""
    return (
        "CONNECTED MCP SERVERS (authoritative, organization-provided tools):\n"
        + "\n".join(lines) + "\n\n"
        "RULES:\n"
        "1. When the user mentions one of these server names, it refers to THAT product - never reinterpret "
        "the name from general knowledge.\n"
        "2. Call a server's tools FIRST only when the question is about the API/integration it covers "
        "(endpoints, parameters, request/response, error codes, SDK or code samples, integration steps) "
        "and base the answer on their results.\n"
        "3. A company or brand name alone is not an API question: for general questions (overview, news, "
        "people, products, 'summarize X') use the company knowledge-base context if provided, then "
        "`web_search` if available - not these tools, and do not put these server/product names into web "
        "search queries unless the user wrote them.\n"
        "4. Never invent endpoints, parameters or credentials; use [PLACEHOLDER] for keys and secrets in code."
    )


def mcp_status(user_id: Optional[int] = None) -> list:
    """Global servers, plus user_id's personal servers when given."""
    return [s.status_info() for s in _servers.values()
            if s.owner is None or (user_id is not None and s.owner == user_id)]


def is_ready(name: str, owner: Optional[int] = None) -> bool:
    s = _servers.get(server_key(name, owner))
    return bool(s and s.status == "ready")


async def connect_one(name: str, cfg: dict, owner: Optional[int] = None) -> dict:
    key = server_key(name, owner)
    existing = _servers.pop(key, None)
    if existing:
        existing.stop()
    registry.unregister_source(f"mcp:{key}")
    srv = McpServer(name, cfg, owner)
    _servers[key] = srv
    if cfg.get("disabled"):
        srv.status = "disabled"
        return srv.status_info()
    tools = await srv.connect()
    if _servers.get(key) is not srv:
        srv.stop()
        return srv.status_info()
    for t in tools:
        tname = t.get("name")
        if not tname:
            continue
        registry.register(
            f"mcp__{name}__{tname}",
            _tool_bridge(srv, tname),
            _bridge_schema(name, t),
            source=f"mcp:{key}",
            meta={"label": f"{name}/{tname}", "read_only": mcp_tool_is_read_only(t)}, replace=True, owner=owner)
    return srv.status_info()


def disconnect_one(name: str, owner: Optional[int] = None) -> None:
    key = server_key(name, owner)
    srv = _servers.pop(key, None)
    if srv:
        srv.stop()
    registry.unregister_source(f"mcp:{key}")


def stop_all_mcp() -> None:
    for s in _servers.values():
        s.stop()
    _servers.clear()
