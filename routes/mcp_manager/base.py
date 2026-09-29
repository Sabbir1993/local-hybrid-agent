from typing import Optional
from fastapi import APIRouter
from core import mcp as mcp_core
from core.deps import require_permission


# global MCP servers and their OAuth tokens are shared by every user of this instance
MANAGE_PERM = "settings.orchestration.configure"


_manage = require_permission(MANAGE_PERM)


# a personal server ("just for me") only needs the ordinary app permission
_use = require_permission("chat.use")


router = APIRouter(prefix="/mcp", tags=["mcp"])


def _server_status(server_id: str) -> Optional[dict]:
    for s in mcp_core.mcp_status():
        if s["name"] == server_id:
            return s
    return None
