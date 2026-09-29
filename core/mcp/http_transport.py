import asyncio
import json
from typing import TYPE_CHECKING, Optional

import httpx

from .constants import RPC_TIMEOUT_S, secret_env_ref

if TYPE_CHECKING:
    from .server import McpServer


async def http_rpc(server: "McpServer", method: str, params: Optional[dict],
                   notify: bool = False, timeout: float = RPC_TIMEOUT_S,
                   _retry_auth: bool = True) -> Optional[dict]:
    from .. import mcp_oauth
    if server._http is None:
        server._http = httpx.AsyncClient(timeout=RPC_TIMEOUT_S)
    server._rpc_id += 1
    rid = server._rpc_id
    msg: dict = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    if not notify:
        msg["id"] = rid
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if isinstance(server.cfg.get("headers"), dict):
        for k, v in server.cfg["headers"].items():
            headers[str(k)] = str(v)
    if isinstance(server.cfg.get("env"), dict):
        for k, v in server.cfg["env"].items():
            if k.upper() == "AUTHORIZATION":
                headers["Authorization"] = str(v)
            elif k.upper() in ("BEARER_TOKEN", "ACCESS_TOKEN"):
                headers["Authorization"] = f"Bearer {str(v)}"
            elif k.upper() == "API_KEY":
                headers["x-api-key"] = str(v)
    if server.cfg.get("secret_env_keys"):
        from .. import credentials
        for key in server.cfg["secret_env_keys"]:
            val = credentials.get_token(secret_env_ref(server.name, key, server.owner))
            if val:
                if key.upper() == "AUTHORIZATION":
                    headers["Authorization"] = val
                elif key.upper() in ("BEARER_TOKEN", "ACCESS_TOKEN"):
                    headers["Authorization"] = f"Bearer {val}"
                elif key.upper() == "API_KEY":
                    headers["x-api-key"] = val
    oauth_tok = await mcp_oauth.access_token(server.name, server.owner)
    if oauth_tok:
        headers["Authorization"] = f"Bearer {oauth_tok}"
    if server._session_header:
        headers["Mcp-Session-Id"] = server._session_header
    if server._protocol_version:
        headers["MCP-Protocol-Version"] = server._protocol_version
    if server.owner is not None:
        from ..net_guard import check_url
        await asyncio.get_running_loop().run_in_executor(None, check_url, server.cfg["url"])
    r = await server._http.post(server.cfg["url"], json=msg, headers=headers, timeout=timeout)
    if r.status_code == 401:
        if oauth_tok and _retry_auth and await mcp_oauth.access_token(server.name, server.owner, force_refresh=True):
            return await http_rpc(server, method, params, notify, timeout, _retry_auth=False)
        server.auth_required = True
        raise mcp_oauth.McpAuthRequired(
            f"'{server.name}' needs you to sign in: Settings -> Capabilities -> MCP -> {server.name} -> Connect account")
    if r.status_code >= 400:
        if server.owner is not None:
            raise RuntimeError(f"mcp http {r.status_code}")
        raise RuntimeError(f"mcp http {r.status_code}: {r.text[:200]}")
    sid = r.headers.get("mcp-session-id")
    if sid:
        server._session_header = sid
    ctype = r.headers.get("content-type", "")
    if "text/event-stream" in ctype:
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
