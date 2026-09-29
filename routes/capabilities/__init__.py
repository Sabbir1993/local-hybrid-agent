"""routes/capabilities - Tool capabilities, shell execution settings, and model routing status."""

from .endpoints import (
    agent_library_get,
    agent_library_update,
    agent_settings,
    capabilities_status,
    capabilities_toggle,
    models_status,
    router as router,
    router_get,
    router_suggestion_decide,
    router_tune,
    router_update,
    shell_settings,
)
from .models import (
    AGENT_STEPS_MAX,
    AGENT_STEPS_MIN,
    AgentLibraryReq,
    AgentSettingsReq,
    CapToggleReq,
    LibraryListReq,
    RouterSettingsReq,
    ShellSettingsReq,
)
from .router_helpers import (
    _LIST_KEYS,
    _MAIN_CATEGORIES,
    _router_settings,
    _router_view,
    _validate_router_changes,
    _write_router,
)

__all__ = [
    "router",
    # models
    "CapToggleReq", "LibraryListReq", "AgentLibraryReq", "ShellSettingsReq",
    "AGENT_STEPS_MIN", "AGENT_STEPS_MAX", "AgentSettingsReq", "RouterSettingsReq",
    # router helpers
    "_LIST_KEYS", "_MAIN_CATEGORIES", "_validate_router_changes", "_router_settings",
    "_write_router", "_router_view",
    # endpoints
    "models_status", "capabilities_status", "shell_settings", "agent_settings",
    "agent_library_get", "agent_library_update", "capabilities_toggle",
    "router_get", "router_update", "router_tune", "router_suggestion_decide",
]
