from typing import Optional
from pydantic import BaseModel


class McpServerReq(BaseModel):
    name: str
    scope: str = "global"             # global | user
    from_scope: Optional[str] = None  # original scope if changing availability on edit
    transport: str = "stdio"          # stdio | http
    command: Optional[str] = None
    args: list = []
    url: Optional[str] = None
    env: dict = {}                    # plain values, stored in config/app.json (global) / auth.db (user)
    secret_env: dict = {}             # values -> OS keychain; blank value on edit = keep existing
    headers: dict = {}                # http only: extra non-secret request headers
    auth: Optional[dict] = None       # http only: {"type": "oauth", client_id, scopes, auth_url?, token_url?, ...}
    disabled: bool = False


class McpImportReq(BaseModel):
    config: dict                      # {"mcpServers": {...}} or the inner mapping
    scope: str = "global"


class UserPackagesReq(BaseModel):
    packages: list


class OAuthStartReq(BaseModel):
    scope: str = "user"
