"""
routes package - Modularized API route handlers for Local Agent.
"""

from .auth import router as auth_router
from .admin_rbac import router as admin_rbac_router
from .control import router as control_router
from .projects import router as projects_router
from .capabilities import router as capabilities_router
from .chat import router as chat_router
from .agent import router as agent_router
from .cloud import router as cloud_router
from .git import router as git_router
from .mcp_manager import router as mcp_manager_router
from .knowledge import router as knowledge_router
from .proxy import router as proxy_router
from .input_guard import router as input_guard_router
from .db_explorer import router as db_explorer_router

__all__ = [
    "auth_router",
    "admin_rbac_router",
    "control_router",
    "projects_router",
    "capabilities_router",
    "chat_router",
    "agent_router",
    "cloud_router",
    "git_router",
    "mcp_manager_router",
    "knowledge_router",
    "proxy_router",
    "input_guard_router",
    "db_explorer_router",
]
