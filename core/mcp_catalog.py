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

# Reviewed connector presets for the Customize page: a fixed transport/command/args (or https url)
# template, installed exactly like a server added by hand (same validation + keychain secrets in
# routes/mcp_manager). Only entries here get an Install button - public-registry results are browse-only.
#   transport   stdio (command/args) or http (url; streamable HTTP, sign-in handled natively by core/mcp_oauth)
#   auth        {"type": "oauth", "scopes": [...]} for http servers that need a sign-in
#   client      who provides the OAuth client: "dcr" (server registers us automatically, confirmed in vendor docs),
#               "auto" (try registration; the admin supplies a client only if the server refuses),
#               "preregistered" (vendor has no registration: an admin creates an app once and pastes its
#               client id/secret - routes/customize/client_endpoints.py), "none" (no sign-in)
#   scope       default install scope: "user" (each person signs in, tools only visible to them) or "global"
#               (one shared account, admin installs). Anything with `sensitivity` is always per user.
#   sensitivity "pii" | "payments" | "data": off until an admin allows it (capabilities.connectors_enabled)
#   fields      values the installer fills into {placeholders} of the url (validated by `pattern`)
#   secret_env  env vars typed on install; values go to the OS keychain
#   egress      True when tool calls leave this host (third-party API): warning + PAN masking (core/mcp)
#   color       brand-neutral tile colour for the card monogram (no third-party logos are bundled)
# Endpoints below were taken from each vendor's own documentation (2026-09-30). Vendors change URLs:
# re-check before relying on one. Left out on purpose (no official public endpoint found): Microsoft 365
# Work IQ (per-tenant Entra app, no public URL), Adobe, Asana, PayPal, v0 (API-key header, unsupported for http),
# MongoDB (local server could be aimed at internal hosts), PDF viewer / Three.js (local MCP-Apps servers that read
# server files or need an inline UI) - add them with Settings -> Capabilities -> MCP until they are confirmed.
def _remote(pid, name, desc, category, author, url, homepage, *, client="auto", scope="global",
            sensitivity=None, scopes=None, note="", color="#64748b", fields=None, auth=True):
    e = {"id": pid, "name": name, "description": desc, "category": category, "author": author,
         "homepage": homepage, "transport": "http", "url": url, "egress": True,
         "client": client if auth else "none", "scope": "user" if sensitivity else scope, "color": color}
    if auth:
        e["auth"] = {"type": "oauth", **({"scopes": scopes} if scopes else {})}
    if sensitivity:
        e["sensitivity"] = sensitivity
    if note:
        e["note"] = note
    if fields:
        e["fields"] = fields
    return e


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
    # No "fetch" preset: mcp-server-fetch runs on this host with no SSRF guard, so any
    # user's agent could reach 127.0.0.1:8090, LAN hosts or cloud metadata. The built-in
    # web_fetch tool covers the same need through core/net_guard.
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
        "id": "shopify-dev",
        "name": "Shopify Dev",
        "description": "Shopify developer docs and GraphQL schemas for building apps and themes. Runs locally; no store access.",
        "category": "developer",
        "author": "Shopify",
        "homepage": "https://shopify.dev/docs/apps/build/devmcp",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@shopify/dev-mcp@latest"],
        "egress": True,
        "color": "#5a863e",
    },
    _remote("microsoft-learn", "Microsoft Learn",
            "Search and read Microsoft and Azure documentation. Read-only, no sign-in.",
            "developer", "Microsoft", "https://learn.microsoft.com/api/mcp",
            "https://learn.microsoft.com/en-us/training/support/mcp", auth=False, color="#2563eb"),
    _remote("atlassian", "Atlassian",
            "Search and update Jira issues and Confluence pages with your own Atlassian account.",
            "tickets", "Atlassian", "https://mcp.atlassian.com/v2/mcp",
            "https://support.atlassian.com/atlassian-rovo-mcp-server/docs/getting-started-with-the-atlassian-remote-mcp-server/",
            scope="user", sensitivity="data", color="#2563eb"),
    _remote("linear", "Linear", "Manage Linear issues, projects and cycles.",
            "tickets", "Linear", "https://mcp.linear.app/mcp", "https://linear.app/docs/mcp",
            client="dcr", color="#6366f1"),
    _remote("notion", "Notion", "Search, read and update pages and databases in your Notion workspace.",
            "productivity", "Notion", "https://mcp.notion.com/mcp",
            "https://developers.notion.com/guides/mcp/get-started-with-mcp",
            scope="user", sensitivity="data", color="#111827"),
    _remote("slack", "Slack", "Search messages and channels, and read threads you can already see in Slack.",
            "communication", "Slack", "https://mcp.slack.com/mcp", "https://docs.slack.dev/ai/mcp-server",
            client="preregistered", sensitivity="pii", color="#7c3aed",
            scopes=["search:read.public", "search:read.private", "search:read.mpim", "search:read.im"],
            note="Slack requires an https redirect address: set capabilities.mcp_oauth_redirect_base, and use a Slack app "
                 "published in the directory or created inside your workspace."),
    _remote("google-drive", "Google Drive", "Search and read files in your Google Drive.",
            "productivity", "Google", "https://drivemcp.googleapis.com/mcp/v1",
            "https://developers.google.com/workspace/guides/configure-mcp-servers",
            client="preregistered", sensitivity="pii", color="#16a34a",
            scopes=["https://www.googleapis.com/auth/drive.readonly", "https://www.googleapis.com/auth/drive.file"],
            note="Google's MCP servers are in a developer preview: enable the Drive API and its MCP service in your Google Cloud project."),
    _remote("gmail", "Gmail", "Search your inbox, summarise threads and draft replies.",
            "communication", "Google", "https://gmailmcp.googleapis.com/mcp/v1",
            "https://developers.google.com/workspace/gmail/api/reference/mcp",
            client="preregistered", sensitivity="pii", color="#dc2626",
            scopes=["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.compose"],
            note="Developer preview. Mail can contain customer and cardholder data: keep it per user and review before enabling."),
    _remote("google-calendar", "Google Calendar", "Read your schedule and find free time.",
            "productivity", "Google", "https://calendarmcp.googleapis.com/mcp/v1",
            "https://developers.google.com/workspace/calendar/api/guides/configure-mcp-server",
            client="preregistered", sensitivity="pii", color="#2563eb",
            scopes=["https://www.googleapis.com/auth/calendar.events.readonly",
                    "https://www.googleapis.com/auth/calendar.events.freebusy"],
            note="Read-only scopes. Creating events needs the calendar.events scope, which is not requested here."),
    _remote("hubspot", "HubSpot", "CRM context for contacts, companies and deals.",
            "crm", "HubSpot", "https://mcp.hubspot.com/", "https://developers.hubspot.com/mcp",
            client="preregistered", sensitivity="pii", color="#ea580c",
            note="Endpoint path not confirmed in HubSpot's docs: check it against developers.hubspot.com/mcp before use."),
    _remote("salesforce", "Salesforce", "Query and update Salesforce records through a hosted MCP server you activated.",
            "crm", "Salesforce", "https://api.salesforce.com/platform/mcp/v1/{server}",
            "https://developer.salesforce.com/docs/platform/hosted-mcp-servers/guide/create-external-client-app.html",
            client="preregistered", sensitivity="pii", color="#0ea5e9", scopes=["mcp_api", "refresh_token"],
            fields=[{"key": "server", "label": "Hosted MCP server name", "placeholder": "platform/sobject-all",
                     "pattern": r"^[A-Za-z0-9_-][A-Za-z0-9_./-]{0,79}$"}],
            note="Create an External Client App with PKCE, activate the hosted server in Setup, then enter its name."),
    _remote("canva", "Canva", "Search, create and export Canva designs.",
            "design", "Canva", "https://mcp.canva.com/mcp", "https://www.canva.dev/docs/apps/mcp/",
            client="dcr", color="#06b6d4"),
    _remote("figma", "Figma", "Read design files and generate code from Figma context.",
            "design", "Figma", "https://mcp.figma.com/mcp",
            "https://developers.figma.com/docs/figma-mcp-server/remote-server-installation/", color="#a855f7"),
    _remote("gamma", "Gamma", "Generate presentations, documents and sites with Gamma.",
            "design", "Gamma", "https://mcp.gamma.app/mcp", "https://developers.gamma.app/mcp/gamma-mcp-server",
            client="dcr", color="#d946ef"),
    _remote("higgsfield", "Higgsfield", "Generate images and video with Higgsfield (uses your Higgsfield credits).",
            "design", "Higgsfield", "https://mcp.higgsfield.ai/mcp",
            "https://higgsfield.ai/creator-hub/help-center/integrations/what-is-higgsfield-mcp",
            scope="user", color="#f43f5e",
            note="Endpoint taken from Higgsfield's help centre; generation spends credits on the signed-in account."),
    _remote("supabase", "Supabase", "Inspect and manage Supabase projects, tables and migrations.",
            "developer", "Supabase", "https://mcp.supabase.com/mcp?read_only=true",
            "https://supabase.com/docs/guides/getting-started/mcp",
            client="dcr", sensitivity="data", color="#22c55e",
            note="Installed read-only. Project data is sensitive: add project_ref=<id> to scope it to one project."),
    _remote("vercel", "Vercel", "Inspect deployments, logs and projects on Vercel.",
            "cloud", "Vercel", "https://mcp.vercel.com", "https://vercel.com/docs/agent-resources/vercel-mcp",
            color="#111827"),
    _remote("sentry", "Sentry", "Search issues and events, and inspect stack traces in Sentry.",
            "engineering", "Sentry", "https://mcp.sentry.dev/mcp", "https://mcp.sentry.dev/",
            color="#7c3aed", note="Event payloads can contain user data."),
    _remote("cloudflare", "Cloudflare", "Manage Workers, DNS and observability on Cloudflare.",
            "cloud", "Cloudflare", "https://mcp.cloudflare.com/mcp",
            "https://developers.cloudflare.com/agents/model-context-protocol/mcp-servers-for-cloudflare/",
            color="#f97316"),
    _remote("windsor", "Windsor.ai", "Query advertising and marketing performance data across connected platforms.",
            "data", "Windsor.ai", "https://mcp.windsor.ai/",
            "https://windsor.ai/documentation/windsor-mcp/how-to-connect-windsor-mcp-to-any-ai-client/",
            client="dcr", sensitivity="data", color="#0891b2",
            note="Has write tools (execute_action): review before enabling."),
    _remote("stripe", "Stripe", "Look up payments, customers and subscriptions in Stripe.",
            "payments", "Stripe", "https://mcp.stripe.com", "https://docs.stripe.com/mcp",
            sensitivity="payments", color="#6366f1",
            note="Payments data. Use a test-mode or restricted account first and complete a PCI-DSS review before real data."),
    _remote("shopify-storefront", "Shopify Storefront", "Browse a store's catalog, cart and policies (public storefront data).",
            "commerce", "Shopify", "https://{shop}/api/mcp", "https://shopify.dev/docs/apps/build/storefront-mcp",
            auth=False, color="#65a30d",
            fields=[{"key": "shop", "label": "Store domain", "placeholder": "my-store.myshopify.com",
                     "pattern": r"^[a-z0-9][a-z0-9-]*(\.[a-z0-9-]+)+$"}]),
    _remote("webflow", "Webflow", "Read and edit Webflow sites, CMS collections and pages.",
            "design", "Webflow", "https://mcp.webflow.com/mcp", "https://developers.webflow.com/mcp/reference/getting-started",
            scope="user", color="#2563eb",
            note="Authorize only the sites the agent needs. CMS and form data can contain customer details."),
    _remote("postman", "Postman", "Browse collections, environments and APIs in your Postman workspace.",
            "developer", "Postman", "https://mcp.postman.com/minimal",
            "https://learning.postman.com/docs/reference/postman-api/postman-mcp-server/postman-mcp-remote-server",
            sensitivity="data", color="#f97316",
            note="Uses the minimal tool set (US region, OAuth). Collections and environments often hold tokens: keep real "
                 "credentials out of shared workspaces. The EU server supports API keys only and is not included."),
    _remote("firecrawl", "Firecrawl", "Scrape, search and extract content from web pages.",
            "web", "Firecrawl", "https://mcp.firecrawl.dev/v2/mcp-oauth", "https://docs.firecrawl.dev/mcp-server",
            scope="user", color="#ea580c",
            note="Pages are fetched by Firecrawl, not by this server. Do not scrape pages that contain cardholder data."),
    _remote("lovable", "Lovable", "List, read and message your Lovable projects.",
            "developer", "Lovable", "https://mcp.lovable.dev", "https://docs.lovable.dev/integrations/lovable-mcp-server",
            scope="user", color="#ec4899",
            note="Actions use your Lovable credits and can publish apps. Enterprise workspaces need an admin to enable "
                 "third-party MCP clients first."),
    _remote("expo", "Expo", "Expo docs, EAS builds and logs for your Expo account.",
            "developer", "Expo", "https://mcp.expo.dev/mcp", "https://docs.expo.dev/mcp/",
            scope="user", color="#111827", note="Usage counts against your Expo plan."),
    _remote("eraser", "Eraser", "Generate and edit diagrams in Eraser from a description.",
            "design", "Eraser", "https://app.eraser.io/api/mcp", "https://docs.eraser.io/mcp",
            scope="user", color="#e11d48",
            note="Diagrams are stored on Eraser. Do not include cardholder data or internal network details."),
    _remote("drawio", "draw.io", "Create diagrams as draw.io XML. No sign-in.",
            "design", "draw.io", "https://mcp.draw.io/mcp", "https://www.drawio.com/docs/manual/generate/drawio-mcp-server/",
            auth=False, color="#f59e0b",
            note="Hosted by draw.io; diagram content leaves this network. Here it returns XML text (no inline preview)."),
    _remote("swagger", "Swagger", "Browse and edit API definitions in SwaggerHub / Swagger Studio.",
            "developer", "SmartBear", "https://swagger.mcp.smartbear.com/mcp",
            "https://developer.smartbear.com/smartbear-mcp/docs/remote-swagger",
            sensitivity="data", color="#16a34a",
            note="API definitions can reveal internal endpoints and sample payloads. Use placeholders, not real data."),
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


# --- install helpers shared by routes/customize ---------------------------------------------

def _caps() -> dict:
    from .small_model import APP_CONFIG
    return APP_CONFIG.get("capabilities", {}) or {}


def connector_allowed(pre: dict) -> bool:
    """May users add this connector? capabilities.connectors_enabled is an explicit allow-list; without it,
    everything except sensitive connectors (personal data, payments, project data) is allowed."""
    enabled = _caps().get("connectors_enabled")
    if isinstance(enabled, list):
        return pre["id"] in enabled
    return not pre.get("sensitivity")


def default_enabled_ids() -> list:
    return [p["id"] for p in PRESETS if not p.get("sensitivity")]


def shared_client_id(preset_id: str) -> str:
    """client_id an admin registered once for this connector (capabilities.mcp_catalog_overrides)."""
    ov = (_caps().get("mcp_catalog_overrides") or {}).get(preset_id) or {}
    return str(ov.get("client_id") or "")


def shared_secret_ref(preset_id: str) -> str:
    return f"oauthapp:{preset_id}:secret"


def shared_client(name: str, cfg: dict) -> Optional[dict]:
    """{client_id, secret} an admin registered for catalog connector `name`, but only for a server that really
    is that connector: same host as the catalog url. A personal server that merely reuses the name (and points
    somewhere else) never receives the shared client secret."""
    from urllib.parse import urlparse
    pre = get_preset(name)
    cid = shared_client_id(name)
    if not pre or not cid or not pre.get("auth"):
        return None
    want = urlparse((pre.get("url") or "").replace("{", "x").replace("}", "x")).hostname
    got = urlparse((cfg or {}).get("url") or "").hostname
    if not want or want != got:
        return None
    from . import credentials
    return {"client_id": cid, "secret": credentials.get_token(shared_secret_ref(name)) or ""}


def resolve_url(pre: dict, values: dict) -> tuple:
    """(url, error): fill the preset url's {placeholders} from the installer's values, each checked
    against the preset's own pattern so nothing else can reach the hostname or path."""
    import re
    url = pre.get("url") or ""
    for f in pre.get("fields") or []:
        val = str((values or {}).get(f["key"]) or "").strip()
        if not val:
            return None, f"{f['label']} is required"
        if not re.match(f["pattern"], val):
            return None, f"{f['label']} doesn't look right (example: {f['placeholder']})"
        url = url.replace("{" + f["key"] + "}", val)
    return url, None


def effective_scope(pre: dict, requested: str, can_install_global: bool) -> tuple:
    """(scope, error). Sensitive connectors are always per user; otherwise the installer chooses,
    defaulting to the preset's scope, and 'global' needs capabilities.install."""
    scope = requested or pre.get("scope") or "global"
    if pre.get("sensitivity"):
        scope = "user"
    if scope not in ("user", "global"):
        return None, "scope must be 'user' or 'global'"
    if scope == "global" and not can_install_global:
        return None, "installing for everyone needs the capabilities.install permission; install it just for you instead"
    return scope, None
