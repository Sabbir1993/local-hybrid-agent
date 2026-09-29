"""All capability and router endpoints.
"""

from .base import (
    router,
)
from .status import (
    models_status,
    capabilities_status,
    capabilities_toggle,
)
from .settings import (
    shell_settings,
    agent_settings,
)
from .library import (
    agent_library_get,
    agent_library_update,
)
from .router_cfg import (
    router_get,
    router_update,
    router_tune,
    router_suggestion_decide,
)

__all__ = [
    "router",
    "models_status",
    "capabilities_status",
    "capabilities_toggle",
    "shell_settings",
    "agent_settings",
    "agent_library_get",
    "agent_library_update",
    "router_get",
    "router_update",
    "router_tune",
    "router_suggestion_decide",
]
