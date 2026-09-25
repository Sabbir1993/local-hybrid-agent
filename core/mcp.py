"""MCP (Model Context Protocol) client: stdio + streamable-HTTP transports.

Config (config/app.json -> capabilities.mcp_servers):
  "mcp_servers": {
    "name": {
      "transport": "stdio" | "http",
      "command": "python", "args": ["-m", "some_mcp_server"],   # stdio
      "env": {"KEY": "val"},
      "secret_env_keys": ["API_KEY"],     # values live in the OS keychain (core/credentials.py)
      "url": "http://127.0.0.1:9000/mcp",                         # http
      "disabled": false, "init_timeout": 120
    }
  }

A top-level Claude-Desktop style "mcpServers" block in app.json is merged in too
(capabilities.mcp_servers wins on a name clash).

Personal servers (one user only) have the same config shape but live in auth.db
(user_mcp_servers); they run as their own process keyed "<name>@u<user id>", and
their tools are registered with an owner so only that user sees them.

Bridged tools appear as mcp__<server>__<tool> in the registry.
"""

import asyncio
import collections
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import httpx

from .registry import registry
from .agent_tools import MAX_TOOL_OUTPUT

MCP_PROTOCOL_VERSION = "2025-03-26"
RPC_TIMEOUT_S = 30
INIT_TIMEOUT_S = 20
NPX_INIT_TIMEOUT_S = 120    # first npx run downloads the package / may wait on an OAuth browser login


class McpServer:
    """One configured MCP server (either transport)."""

    def __init__(self, name: str, cfg: dict, owner: Optional[int] = None):
        self.name = name
        self.owner = owner          # user id for a personal server, None = global
        self.key = server_key(name, owner)
        self.cfg = cfg or {}
        self.transport = (cfg.get("transport") or ("stdio" if cfg.get("command") else "http")).lower()
        self.status = "idle"        # idle|connecting|ready|error|stopped
        self.error: Optional[str] = None
        self.tools: list = []
        self._proc: Optional[subprocess.Popen] = None
        self._reader = None
        self._writer = None
        self._http: Optional[httpx.AsyncClient] = None
        self._rpc_id = 0
        self._lock = asyncio.Lock()
        self._session_header: Optional[str] = None   # streamable-http session id
        self._stderr_tail: collections.deque = collections.deque(maxlen=50)

    # ---------------- stdio plumbing ----------------

    def _spawn_stdio(self) -> None:
        command = str(self.cfg["command"])
        # Windows: npx/uvx are .cmd shims that Popen can't find without a shell; which() applies PATHEXT
        resolved = shutil.which(command)
        if not resolved:
            raise RuntimeError(f"command '{command}' not found on PATH")
        cmd = [resolved] + [str(a) for a in (self.cfg.get("args") or [])]
        env = None
        if self.cfg.get("env"):
            env = {**os.environ, **{str(k): str(v) for k, v in self.cfg["env"].items()}}
        if self.cfg.get("credential_ref") == "keyring" and self.owner is None:
            env = self._inject_keyring_token(env)
        if self.cfg.get("secret_env_keys"):
            env = self._inject_secret_env(env)
        self._stderr_tail.clear()
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace", env=env, bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        # drain stderr, else a chatty server (mcp-remote) fills the pipe and blocks
        threading.Thread(target=self._drain_stderr, args=(self._proc,), daemon=True).start()

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        try:
            for line in proc.stderr:
                line = line.rstrip()
                if line:
                    self._stderr_tail.append(line[:300])
        except Exception:
            pass

    def _stderr_summary(self, n: int = 6) -> str:
        return " | ".join(list(self._stderr_tail)[-n:])

    def _inject_secret_env(self, env: Optional[dict]) -> Optional[dict]:
        """Secret env vars added via the Settings UI: values in the OS keychain, only key names in app.json."""
        from . import credentials
        base = env if env is not None else dict(os.environ)
        for key in self.cfg.get("secret_env_keys") or []:
            val = credentials.get_token(secret_env_ref(self.name, key, self.owner))
            if val:
                base[str(key)] = val
        return base

    def _inject_keyring_token(self, env: Optional[dict]) -> Optional[dict]:
        """For servers authorized via the connector catalog's device-flow, pull the token
        out of the OS keychain and inject it under the catalog's env_key. Never touches
        config/app.json - see core/credentials.py and core/mcp_catalog.py."""
        from . import credentials, mcp_catalog
        entry = mcp_catalog.get_entry(self.name)
        token = credentials.get_token(self.name)
        if not entry or not token:
            return env
        env_key = (entry.get("auth") or {}).get("env_key")
        if not env_key:
            return env
        base = env if env is not None else dict(os.environ)
        base[env_key] = token
        return base

    async def _stdio_rpc(self, method: str, params: Optional[dict], notify: bool = False,
                         timeout: float = RPC_TIMEOUT_S) -> Optional[dict]:
        """One JSON-RPC round-trip over stdio (newline-delimited JSON)."""
        assert self._proc and self._proc.stdin and self._proc.stdout
        self._rpc_id += 1
        rid = self._rpc_id
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            msg["id"] = rid
        self._proc.stdin.write(json.dumps(msg) + "\n")
        self._proc.stdin.flush()
        if notify:
            return None
        # read lines until our id comes back (skip notifications)
        loop = asyncio.get_event_loop()
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                line = await asyncio.wait_for(
                    loop.run_in_executor(None, self._proc.stdout.readline),
                    timeout=max(0.1, deadline - time.time()))
            except asyncio.TimeoutError:
                break
            if not line:
                raise RuntimeError(f"mcp server '{self.name}' closed stdout")
            line = line.strip()
            if not line:
                continue
            try:
                resp = json.loads(line)
            except json.JSONDecodeError:
                continue   # server noise on stdout
            if isinstance(resp, dict) and resp.get("id") == rid:
                if isinstance(resp.get("error"), dict):
                    raise RuntimeError(f"mcp error: {resp['error'].get('message')}")
                return resp.get("result")
        # the executor thread is still blocked in readline(); if we kept the process,
        # it would swallow the next response and desync every later call
        self.status = "error"
        self.error = f"rpc timeout waiting for id {rid}"
        self._cleanup()
        raise TimeoutError(f"mcp rpc timeout waiting for id {rid}")

    # ---------------- streamable-http plumbing ----------------

    async def _http_rpc(self, method: str, params: Optional[dict], notify: bool = False,
                        timeout: float = RPC_TIMEOUT_S) -> Optional[dict]:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=RPC_TIMEOUT_S)
        self._rpc_id += 1
        rid = self._rpc_id
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            msg["id"] = rid
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_header:
            headers["Mcp-Session-Id"] = self._session_header
        if self.owner is not None:
            from .net_guard import check_url
            await asyncio.get_running_loop().run_in_executor(None, check_url, self.cfg["url"])
        r = await self._http.post(self.cfg["url"], json=msg, headers=headers, timeout=timeout)
        if r.status_code >= 400:
            if self.owner is not None:   # don't echo arbitrary upstream bodies to a non-admin
                raise RuntimeError(f"mcp http {r.status_code}")
            raise RuntimeError(f"mcp http {r.status_code}: {r.text[:200]}")
        sid = r.headers.get("mcp-session-id")
        if sid:
            self._session_header = sid
        ctype = r.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            # parse SSE body for the response with our id
            for block in r.text.split("\n\n"):
                for ln in block.splitlines():
                    if ln.startswith("data:"):
                        try:
                            resp = json.loads(ln[5:].strip())
                        except json.JSONDecodeError:
                            continue
                        if isinstance(resp, dict) and resp.get("id") == rid:
                            if isinstance(resp.get("error"), dict):
                                raise RuntimeError(f"mcp error: {resp['error'].get('message')}")
                            return resp.get("result")
            return None
        if notify:
            return None
        resp = r.json()
        if isinstance(resp, dict) and isinstance(resp.get("error"), dict):
            raise RuntimeError(f"mcp error: {resp['error'].get('message')}")
        return resp.get("result")

    # ---------------- shared lifecycle ----------------

    async def _rpc(self, method: str, params: Optional[dict] = None, notify: bool = False,
                   timeout: float = RPC_TIMEOUT_S) -> Optional[dict]:
        if self.transport == "stdio":
            return await self._stdio_rpc(method, params, notify, timeout)
        return await self._http_rpc(method, params, notify, timeout)

    def _init_timeout(self) -> float:
        if self.cfg.get("init_timeout"):
            return float(self.cfg["init_timeout"])
        cmd = Path(str(self.cfg.get("command") or "")).stem.lower()
        return NPX_INIT_TIMEOUT_S if cmd in ("npx", "uvx") else INIT_TIMEOUT_S

    async def connect(self) -> list:
        """initialize handshake + tools/list. Returns tool list; sets .status."""
        async with self._lock:
            return await self._connect_locked()

    async def _connect_locked(self) -> list:
        # caller holds self._lock (asyncio.Lock is not reentrant)
        if self.status == "ready":
            return self.tools
        self.status = "connecting"
        self.error = None
        try:
            if self.transport == "stdio":
                self._spawn_stdio()
            result = await self._rpc("initialize", {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "a770-runtime", "version": "1.0"},
            }, timeout=self._init_timeout())
            if not result:
                raise RuntimeError("no initialize result")
            await self._rpc("notifications/initialized", notify=True)
            tools_res = await self._rpc("tools/list", {})
            self.tools = (tools_res or {}).get("tools", []) if isinstance(tools_res, dict) else []
            self.status = "ready"
            print(f"[mcp] server '{self.name}' ready with {len(self.tools)} tool(s)")
            return self.tools
        except Exception as e:
            self.status = "error"
            self.error = str(e) or type(e).__name__
            tail = self._stderr_summary()
            if tail:
                self.error += f" - stderr: {tail}"
            self._cleanup()
            print(f"[mcp] server '{self.name}' failed: {e}", file=sys.stderr)
            return []

    async def call_tool(self, tool_name: str, args: dict) -> str:
        async with self._lock:
            if self.status != "ready":
                await self._connect_locked()
            if self.status != "ready":
                return f"error: mcp server '{self.name}' unavailable: {self.error}"
        try:
            async with self._lock:
                result = await self._rpc("tools/call", {
                    "name": tool_name,
                    "arguments": _mask_args(args or {}),
                })
        except Exception as e:
            return f"error: mcp call failed: {type(e).__name__}: {e}"
        return _stringify_content(result)

    def _cleanup(self) -> None:
        if self._proc:
            proc, self._proc = self._proc, None
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    if pipe:
                        pipe.close()
                except Exception:
                    pass
        if self._http:
            try:
                asyncio.get_event_loop().create_task(self._http.aclose())
            except Exception:
                pass
            self._http = None

    def stop(self) -> None:
        self._cleanup()
        self.status = "stopped"

    def status_info(self) -> dict:
        return {
            "name": self.name,
            "scope": "global" if self.owner is None else "user",
            "transport": self.transport,
            "status": self.status,
            "error": self.error,
            "tools": [
                {"name": t.get("name", "?"),
                 "description": (t.get("description") or "")[:120]}
                for t in self.tools
            ],
        }


def _mask_args(obj):
    """PCI-DSS: tool arguments leave this host (often to a third-party API), so card
    numbers the model put into them are masked like any other egress."""
    from . import pan
    if isinstance(obj, str):
        return pan.mask_pans(obj)[0] if pan.enabled("pan_cloud_egress") else obj
    if isinstance(obj, dict):
        return {k: _mask_args(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask_args(v) for v in obj]
    return obj


def _stringify_content(result) -> str:
    """MCP tool result -> plain string for the LLM."""
    if result is None:
        return "(no result)"
    content = result.get("content") if isinstance(result, dict) else result
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict):
                if c.get("type") == "text":
                    parts.append(c.get("text", ""))
                else:
                    parts.append(json.dumps(c))
            else:
                parts.append(str(c))
        out = "\n".join(p for p in parts if p)
    elif isinstance(content, str):
        out = content
    else:
        out = json.dumps(result)
    if len(out) > MAX_TOOL_OUTPUT:
        out = out[:MAX_TOOL_OUTPUT] + f"\n... (truncated, {len(out)} chars total)"
    # PCI-DSS: external tool output (e.g. payment-gateway lookups) must not put raw PANs in model context
    from . import pan
    out, _ = pan.mask_pans(out)
    return out or "(empty result)"


def secret_env_ref(server_name: str, key: str, owner: Optional[int] = None) -> str:
    """Keychain id for a secret env var of a UI-added server (personal ones are per user)."""
    if owner is None:
        return f"{server_name}:env:{key}"
    return f"u{owner}:{server_name}:env:{key}"


def server_key(name: str, owner: Optional[int] = None) -> str:
    """Runtime id: a personal server never collides with a global one or another user's."""
    return name if owner is None else f"{name}@u{owner}"


def user_servers(user_id: int) -> dict:
    """One user's personal servers: name -> config."""
    from . import auth_db
    return {name: scfg for _, name, scfg in auth_db.list_user_mcp_servers(user_id)}


def configured_servers(app_config: dict) -> dict:
    """capabilities.mcp_servers merged over a top-level Claude-Desktop style mcpServers block."""
    merged = {}
    for name, scfg in (app_config.get("mcpServers") or {}).items():
        if isinstance(scfg, dict):
            merged[name] = scfg
    merged.update(app_config.get("capabilities", {}).get("mcp_servers", {}) or {})
    return merged


# ---------------- manager ----------------

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
    from .small_model import APP_CONFIG
    cfg = APP_CONFIG.get("capabilities", {})
    if not cfg.get("mcp", False):
        return {}
    from . import auth_db
    # concurrently: one slow npx/mcp-remote start must not hold up the others
    targets = [(n, s, None) for n, s in configured_servers(APP_CONFIG).items()]
    targets += [(n, s, uid) for uid, n, s in auth_db.list_user_mcp_servers()]
    infos = await asyncio.gather(*(connect_one(n, s, uid) for n, s, uid in targets))
    return {server_key(n, uid): info for (n, _, uid), info in zip(targets, infos)}


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
    from .request_context import get_current_user_id
    uid = get_current_user_id()
    glob = {s.name for s in _servers.values() if s.owner is None}
    return [s for s in _servers.values()
            if s.owner is None or (s.owner == uid and uid is not None and s.name not in glob)]


def chat_prompt() -> str:
    """Tells the model which MCP servers are connected and what they cover, so a short
    server name like 'isms' isn't guessed from general knowledge (e.g. as an ISO 27001 ISMS)."""
    lines = []
    for s in _visible_servers():
        if s.status != "ready" or not s.tools:
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
    """(Re)connect a single server and register its tools - used by the connector-catalog
    authorize flow so a newly-authorized server comes online without a full app restart."""
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
        # removed or re-saved from the UI while this connect was in flight
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
            meta={"label": f"{name}/{tname}"}, replace=True, owner=owner)
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
