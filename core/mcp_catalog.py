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

CATALOG = []

# Reviewed connector presets for the Customize page: a fixed transport/command/args
# template, installed into capabilities.mcp_servers exactly like a server added by hand
# (same command allowlist + keychain secrets in routes/mcp_manager.py). Only entries here
# get an Install button - public-registry results are browse-only.
#   secret_env: env vars the admin types on install; values go to the OS keychain
#   egress:     True when tool calls leave this host (third-party API) - shown as a warning,
#               since MCP tool *arguments* are not PAN-masked (only results are, core/mcp.py)
PRESETS = [
    {
        "id": "sequential-thinking",
        "name": "Sequential Thinking",
        "description": "Structured step-by-step reasoning scratchpad for long multi-step problems. Runs locally, no network.",
        "category": "productivity",
        "author": "Model Context Protocol",
        "homepage": "https://github.com/modelcontextprotocol/servers/tree/main/src/sequentialthinking",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"],
        "egress": False,
    },
    {
        "id": "time",
        "name": "Time",
        "description": "Current time and timezone conversion (e.g. Asia/Dhaka ↔ UTC for settlement cut-offs). Runs locally.",
        "category": "productivity",
        "author": "Model Context Protocol",
        "homepage": "https://github.com/modelcontextprotocol/servers/tree/main/src/time",
        "transport": "stdio",
        "command": "uvx",
        "args": ["mcp-server-time", "--local-timezone=Asia/Dhaka"],
        "egress": False,
    },
    {
        "id": "fetch",
        "name": "Fetch",
        "description": "Fetch a web page and convert it to markdown for the model to read.",
        "category": "web",
        "author": "Model Context Protocol",
        "homepage": "https://github.com/modelcontextprotocol/servers/tree/main/src/fetch",
        "transport": "stdio",
        "command": "uvx",
        "args": ["mcp-server-fetch"],
        "egress": True,
    },
    {
        "id": "github",
        "name": "GitHub",
        "description": "Read repositories, issues and pull requests; open PRs. Uses a fine-grained personal access token.",
        "category": "engineering",
        "author": "Model Context Protocol",
        "homepage": "https://github.com/modelcontextprotocol/servers-archived/tree/main/src/github",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "secret_env": [{"key": "GITHUB_PERSONAL_ACCESS_TOKEN", "label": "Fine-grained personal access token"}],
        "egress": True,
    },
    {
        "id": "context7",
        "name": "Context7",
        "description": "Up-to-date library and framework documentation lookups for coding tasks.",
        "category": "engineering",
        "author": "Upstash",
        "homepage": "https://github.com/upstash/context7",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@upstash/context7-mcp"],
        "egress": True,
    },
    {
        "id": "atlassian",
        "name": "Atlassian",
        "description": "Search and update Jira issues and Confluence pages. Sign-in opens a browser on the server host (OAuth via mcp-remote).",
        "category": "tickets",
        "author": "Atlassian",
        "homepage": "https://support.atlassian.com/rovo/docs/getting-started-with-the-atlassian-remote-mcp-server/",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "mcp-remote", "https://mcp.atlassian.com/v1/sse"],
        "egress": True,
    },
    {
        "id": "linear",
        "name": "Linear",
        "description": "Manage Linear issues, projects and cycles. Sign-in opens a browser on the server host (OAuth via mcp-remote).",
        "category": "tickets",
        "author": "Linear",
        "homepage": "https://linear.app/docs/mcp",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "mcp-remote", "https://mcp.linear.app/sse"],
        "egress": True,
    },
]


def get_preset(preset_id: str) -> Optional[dict]:
    return next((p for p in PRESETS if p["id"] == preset_id), None)


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
