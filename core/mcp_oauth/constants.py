from typing import Optional

TOKEN_KEY = "OAUTH_TOKEN"
SECRET_KEY = "OAUTH_CLIENT_SECRET"
DCR_KEY = "OAUTH_CLIENT"             # dynamically registered client info (JSON)
DEFAULT_DCR_PORT = 33418             # DCR needs a redirect URI known before the flow starts
FLOW_TTL_S = 600
REFRESH_MARGIN_S = 60
HTTP_TIMEOUT_S = 20

_GOOGLE_HOSTS = ("accounts.google.com", "oauth2.googleapis.com")
REDIRECT_PLACEHOLDER = "__A770_REDIRECT_URI__"

_ID_KEYS = {"clientid", "client_id", "oauth_client_id", "google_client_id"}
_SECRET_KEYS = {"clientsecret", "client_secret", "oauth_client_secret", "google_client_secret"}


class McpAuthRequired(RuntimeError):
    """The MCP server wants a (fresh) user sign-in."""


def _ref(name: str, owner: Optional[int], key: str) -> str:
    from ..mcp import secret_env_ref
    return secret_env_ref(name, key, owner)


def auth_cfg(cfg: dict) -> dict:
    a = (cfg or {}).get("auth")
    return a if isinstance(a, dict) and a.get("type") == "oauth" else {}
