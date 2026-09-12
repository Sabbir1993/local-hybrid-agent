"""MCP (Model Context Protocol) client: stdio + streamable-HTTP transports.

Config (config.json -> capabilities.mcp_servers):
  "mcp_servers": {
    "name": {
      "transport": "stdio" | "http",
      "command": "python", "args": ["-m", "some_mcp_server"],   # stdio
      "env": {"KEY": "val"},
      "url": "http://127.0.0.1:9000/mcp"                          # http
    }
  }

Bridged tools appear as mcp__<server>__<tool> in the registry.
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import httpx

from .registry import registry
from .agent_tools import MAX_TOOL_OUTPUT

MCP_PROTOCOL_VERSION = "2025-03-26"
RPC_TIMEOUT_S = 30
INIT_TIMEOUT_S = 20


class McpServer:
    """One configured MCP server (either transport)."""

    def __init__(self, name: str, cfg: dict):
        self.name = name
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

    # ---------------- stdio plumbing ----------------

    def _spawn_stdio(self) -> None:
        cmd = [str(self.cfg["command"])] + [str(a) for a in (self.cfg.get("args") or [])]
        env = None
        if self.cfg.get("env"):
            env = {**os.environ, **{str(k): str(v) for k, v in self.cfg["env"].items()}}
        # Windows: pipes without shell; text mode utf-8
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace", env=env, bufsize=1)

    async def _stdio_rpc(self, method: str, params: Optional[dict], notify: bool = False) -> Optional[dict]:
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
        deadline = time.time() + RPC_TIMEOUT_S
        while time.time() < deadline:
            line = await loop.run_in_executor(None, self._proc.stdout.readline)
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
        raise TimeoutError(f"mcp rpc timeout waiting for id {rid}")

    # ---------------- streamable-http plumbing ----------------

    async def _http_rpc(self, method: str, params: Optional[dict], notify: bool = False) -> Optional[dict]:
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
        r = await self._http.post(self.cfg["url"], json=msg, headers=headers)
        if r.status_code >= 400:
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

    async def _rpc(self, method: str, params: Optional[dict] = None, notify: bool = False) -> Optional[dict]:
        if self.transport == "stdio":
            return await self._stdio_rpc(method, params, notify)
        return await self._http_rpc(method, params, notify)

    async def connect(self) -> list:
        """initialize handshake + tools/list. Returns tool list; sets .status."""
        async with self._lock:
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
                })
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
                self.error = str(e)
                self._cleanup()
                print(f"[mcp] server '{self.name}' failed: {e}", file=sys.stderr)
                return []

    async def call_tool(self, tool_name: str, args: dict) -> str:
        async with self._lock:
            if self.status != "ready":
                await self.connect()
            if self.status != "ready":
                return f"error: mcp server '{self.name}' unavailable: {self.error}"
        try:
            async with self._lock:
                result = await self._rpc("tools/call", {
                    "name": tool_name,
                    "arguments": args or {},
                })
        except Exception as e:
            return f"error: mcp call failed: {type(e).__name__}: {e}"
        return _stringify_content(result)

    def _cleanup(self) -> None:
        if self._proc:
            try:
                self._proc.kill()
            except Exception:
                pass
            self._proc = None
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
            "transport": self.transport,
            "status": self.status,
            "error": self.error,
            "tools": [
                {"name": t.get("name", "?"),
                 "description": (t.get("description") or "")[:120]}
                for t in self.tools
            ],
        }


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
    return out or "(empty result)"


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
    servers_cfg = cfg.get("mcp_servers", {}) or {}
    results = {}
    for name, scfg in servers_cfg.items():
        srv = McpServer(name, scfg)
        _servers[name] = srv
        tools = await srv.connect()
        for t in tools:
            tname = t.get("name")
            if not tname:
                continue
            registry.register(
                f"mcp__{name}__{tname}",
                _tool_bridge(srv, tname),
                _bridge_schema(name, t),
                source=f"mcp:{name}",
                meta={"label": f"{name}/{tname}"}, replace=True)
        results[name] = srv.status_info()
    return results


def mcp_status() -> list:
    return [s.status_info() for s in _servers.values()]


def stop_all_mcp() -> None:
    for s in _servers.values():
        s.stop()
    _servers.clear()
