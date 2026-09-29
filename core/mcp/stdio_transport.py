import asyncio
import collections
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING, Optional

from .constants import RPC_TIMEOUT_S, secret_env_ref

if TYPE_CHECKING:
    from .server import McpServer


def inject_secret_env(server: "McpServer", env: Optional[dict]) -> Optional[dict]:
    """Secret env vars added via the Settings UI: values in the OS keychain, only key names in app.json."""
    from .. import credentials
    base = env if env is not None else dict(os.environ)
    for key in server.cfg.get("secret_env_keys") or []:
        val = credentials.get_token(secret_env_ref(server.name, key, server.owner))
        if val:
            base[str(key)] = val
    return base


def inject_keyring_token(server: "McpServer", env: Optional[dict]) -> Optional[dict]:
    """For servers authorized via the connector catalog's device-flow, pull the token
    out of the OS keychain and inject it under the catalog's env_key."""
    from .. import credentials, mcp_catalog
    entry = mcp_catalog.get_entry(server.name)
    token = credentials.get_token(server.name)
    if not entry or not token:
        return env
    env_key = (entry.get("auth") or {}).get("env_key")
    if not env_key:
        return env
    base = env if env is not None else dict(os.environ)
    base[env_key] = token
    return base


def spawn_stdio(server: "McpServer") -> subprocess.Popen:
    shutil_mod = getattr(sys.modules.get("core.mcp"), "shutil", shutil)
    subp_mod = getattr(sys.modules.get("core.mcp"), "subprocess", subprocess)
    command = str(server.cfg["command"])
    resolved = shutil_mod.which(command)
    if not resolved:
        raise RuntimeError(f"command '{command}' not found on PATH")
    cmd = [resolved] + [str(a) for a in (server.cfg.get("args") or [])]
    env = None
    if server.cfg.get("env"):
        env = {**os.environ, **{str(k): str(v) for k, v in server.cfg["env"].items()}}
    if server.cfg.get("credential_ref") == "keyring" and server.owner is None:
        env = inject_keyring_token(server, env)
    if server.cfg.get("secret_env_keys"):
        env = inject_secret_env(server, env)
    server._stderr_tail.clear()
    proc = subp_mod.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8",
        errors="replace", env=env, bufsize=1,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    threading_mod = getattr(sys.modules.get("core.mcp"), "threading", threading)
    threading_mod.Thread(target=drain_stderr, args=(server, proc), daemon=True).start()
    return proc


def drain_stderr(server: "McpServer", proc: subprocess.Popen) -> None:
    try:
        for line in proc.stderr:
            line = line.rstrip()
            if line:
                server._stderr_tail.append(line[:300])
    except Exception:
        pass


async def stdio_rpc(server: "McpServer", method: str, params: Optional[dict],
                    notify: bool = False, timeout: float = RPC_TIMEOUT_S) -> Optional[dict]:
    """One JSON-RPC round-trip over stdio (newline-delimited JSON)."""
    assert server._proc and server._proc.stdin and server._proc.stdout
    server._rpc_id += 1
    rid = server._rpc_id
    msg: dict = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    if not notify:
        msg["id"] = rid
    server._proc.stdin.write(json.dumps(msg) + "\n")
    server._proc.stdin.flush()
    if notify:
        return None
    loop = asyncio.get_event_loop()
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            line = await asyncio.wait_for(
                loop.run_in_executor(None, server._proc.stdout.readline),
                timeout=max(0.1, deadline - time.time()))
        except asyncio.TimeoutError:
            break
        if not line:
            raise RuntimeError(f"mcp server '{server.name}' closed stdout")
        line = line.strip()
        if not line:
            continue
        try:
            resp = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(resp, dict) and resp.get("id") == rid:
            if isinstance(resp.get("error"), dict):
                raise RuntimeError(f"mcp error: {resp['error'].get('message')}")
            return resp.get("result")
    server.status = "error"
    server.error = f"rpc timeout waiting for id {rid}"
    server._cleanup()
    raise TimeoutError(f"mcp rpc timeout waiting for id {rid}")
