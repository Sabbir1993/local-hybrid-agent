"""
routes package - Modularized API route handlers for A770-Dual Runtime.
"""

from .control import router as control_router
from .projects import router as projects_router
from .capabilities import router as capabilities_router
from .chat import router as chat_router
from .agent import router as agent_router
from .proxy import router as proxy_router

__all__ = [
    "control_router",
    "projects_router",
    "capabilities_router",
    "chat_router",
    "agent_router",
    "proxy_router",
]
