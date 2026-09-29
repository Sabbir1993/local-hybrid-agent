import asyncio
import collections
import subprocess
import sys
from pathlib import Path
from typing import Optional

import httpx

from .constants import (
    INIT_TIMEOUT_S,
    MCP_PROTOCOL_VERSION,
    NPX_INIT_TIMEOUT_S,
    RPC_TIMEOUT_S,
    _mask_args,
    _signed_in,
    _stringify_content,
    server_key,
)
from .http_transport import http_rpc
from .stdio_transport import (
    drain_stderr,
    inject_keyring_token,
    inject_secret_env,
    spawn_stdio,
    stdio_rpc,
)


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
        self._protocol_version: Optional[str] = None  # negotiated in initialize (MCP-Protocol-Version header)
        self.auth_required = False                    # last call hit 401: user must (re)connect via OAuth
        self._stderr_tail: collections.deque = collections.deque(maxlen=50)

    def _spawn_stdio(self) -> None:
        self._proc = spawn_stdio(self)

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        drain_stderr(self, proc)

    def _stderr_summary(self, n: int = 6) -> str:
        return " | ".join(list(self._stderr_tail)[-n:])

    def _inject_secret_env(self, env: Optional[dict]) -> Optional[dict]:
        return inject_secret_env(self, env)

    def _inject_keyring_token(self, env: Optional[dict]) -> Optional[dict]:
        return inject_keyring_token(self, env)

    async def _stdio_rpc(self, method: str, params: Optional[dict], notify: bool = False,
                         timeout: float = RPC_TIMEOUT_S) -> Optional[dict]:
        return await stdio_rpc(self, method, params, notify, timeout)

    async def _http_rpc(self, method: str, params: Optional[dict], notify: bool = False,
                        timeout: float = RPC_TIMEOUT_S, _retry_auth: bool = True) -> Optional[dict]:
        return await http_rpc(self, method, params, notify, timeout, _retry_auth)

    async def _rpc(self, method: str, params: Optional[dict] = None, notify: bool = False,
                   timeout: float = RPC_TIMEOUT_S) -> Optional[dict]:
        if self.transport == "stdio":
            return await self._stdio_rpc(method, params, notify, timeout)
        return await self._http_rpc(method, params, notify, timeout)

    def _init_timeout(self) -> float:
        if self.cfg.get("init_timeout"):
            return float(self.cfg["init_timeout"])
        cmd = Path(str(self.cfg.get("command") or "")).stem.lower()
        heads = [Path(str(a)).stem.lower() for a in (self.cfg.get("args") or [])[:3]]
        slow = cmd in ("npx", "uvx") or (cmd in ("cmd", "powershell", "pwsh") and any(h in ("npx", "uvx") for h in heads))
        return NPX_INIT_TIMEOUT_S if slow else INIT_TIMEOUT_S

    async def connect(self) -> list:
        """initialize handshake + tools/list. Returns tool list; sets .status."""
        async with self._lock:
            return await self._connect_locked()

    async def _connect_locked(self) -> list:
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
            if isinstance(result, dict) and result.get("protocolVersion"):
                self._protocol_version = str(result["protocolVersion"])
            await self._rpc("notifications/initialized", notify=True)
            self.tools = await self._list_tools()
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

    async def _list_tools(self) -> list:
        tools, cursor = [], None
        for _ in range(20):
            res = await self._rpc("tools/list", {"cursor": cursor} if cursor else {})
            if not isinstance(res, dict):
                break
            tools += res.get("tools") or []
            cursor = res.get("nextCursor")
            if not cursor:
                break
        return tools

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
            from ..mcp_oauth import McpAuthRequired
            if isinstance(e, McpAuthRequired):
                return f"error: {e}"
            return f"error: mcp call failed: {type(e).__name__}: {e}"
        self.auth_required = False
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
            "auth": "oauth" if (self.cfg.get("auth") or {}).get("type") == "oauth" else None,
            "auth_required": self.auth_required,
            "signed_in": self.transport == "http" and _signed_in(self.name, self.owner),
            "tools": [
                {"name": t.get("name", "?"),
                 "description": (t.get("description") or "")[:120]}
                for t in self.tools
            ],
        }
