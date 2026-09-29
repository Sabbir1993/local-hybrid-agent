import json
from typing import Optional

from ..agent_tools import MAX_TOOL_OUTPUT

MCP_PROTOCOL_VERSION = "2025-03-26"
RPC_TIMEOUT_S = 30
INIT_TIMEOUT_S = 20
NPX_INIT_TIMEOUT_S = 120    # first npx run downloads the package / may wait on an OAuth browser login


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
    from .. import auth_db
    return {name: scfg for _, name, scfg in auth_db.list_user_mcp_servers(user_id)}


def configured_servers(app_config: dict) -> dict:
    """capabilities.mcp_servers merged over a top-level Claude-Desktop style mcpServers block."""
    merged = {}
    for name, scfg in (app_config.get("mcpServers") or {}).items():
        if isinstance(scfg, dict):
            merged[name] = scfg
    merged.update(app_config.get("capabilities", {}).get("mcp_servers", {}) or {})
    return merged


def _signed_in(name: str, owner: Optional[int]) -> bool:
    from .. import mcp_oauth
    try:
        return mcp_oauth.has_token(name, owner)
    except Exception:
        return False


def _mask_args(obj):
    """PCI-DSS: tool arguments leave this host (often to a third-party API), so card
    numbers the model put into them are masked like any other egress."""
    from .. import pan
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
    from .. import pan
    out, _ = pan.mask_pans(out)
    return out or "(empty result)"
