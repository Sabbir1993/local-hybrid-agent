"""Known MCP connectors this app can one-click authorize + connect.

This is a small local overlay (transport/command/args + device-flow auth config) for the
handful of providers we wire up one-click auth for - starting with GitHub, for PR creation
from the Source Control panel. The public MCP Registry (registry.modelcontextprotocol.io)
lists far more servers but doesn't know each one's auth requirements, so it's used only for
"browse more" (see fetch_public_registry) - one-click Connect only exists for entries below.

To enable GitHub, register a minimal GitHub OAuth App with "Device Flow" enabled
(https://github.com/settings/developers) and put its Client ID in config/app.json under
capabilities.mcp_catalog_overrides.github.client_id. No client secret is needed or used.
"""

from typing import Optional

import httpx

PUBLIC_REGISTRY_URL = "https://registry.modelcontextprotocol.io/v0/servers"
REGISTRY_TIMEOUT_S = 15

CATALOG = [
    {
        "id": "github",
        "name": "GitHub",
        "description": "Create pull requests, read repos/issues via the GitHub MCP server.",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "auth": {
            "type": "device_flow",
            "client_id": "",  # filled in from config/app.json capabilities.mcp_catalog_overrides.github.client_id
            "device_code_url": "https://github.com/login/device/code",
            "token_url": "https://github.com/login/oauth/access_token",
            "scope": "repo",
            "env_key": "GITHUB_PERSONAL_ACCESS_TOKEN",
        },
    },
]


def catalog_with_overrides() -> list:
    """Merge user-supplied overrides (currently just client_id) from config/app.json."""
    from .small_model import APP_CONFIG
    overrides = (APP_CONFIG.get("capabilities", {}) or {}).get("mcp_catalog_overrides", {}) or {}
    out = []
    for entry in CATALOG:
        e = dict(entry)
        e["auth"] = dict(entry.get("auth") or {})
        ov = overrides.get(e["id"]) or {}
        if ov.get("client_id"):
            e["auth"]["client_id"] = ov["client_id"]
        out.append(e)
    return out


def get_entry(server_id: str) -> Optional[dict]:
    for e in catalog_with_overrides():
        if e["id"] == server_id:
            return e
    return None


async def fetch_public_registry(search: Optional[str] = None) -> dict:
    """Browse-only lookup against the public MCP Registry. Never used for one-click auth."""
    params = {"search": search} if search else {}
    try:
        async with httpx.AsyncClient(timeout=REGISTRY_TIMEOUT_S) as client:
            r = await client.get(PUBLIC_REGISTRY_URL, params=params)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        return {"error": f"registry lookup failed: {e}"}
