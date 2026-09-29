"""routes/control.py - Server management, configuration, profiling, and monitoring endpoints.
"""

from .base import (
    router,
)
from .models import (
    SwitchRequest,
    KeepaliveRequest,
    ConfigRequest,
)
from .config_helpers import (
    _apply_config_update,
    check_tool_calling,
    _reasoning_caps,
    _config_for_profile,
    _standalone_profile,
)
from .config_endpoints import (
    get_config,
    set_config,
)
from .status_endpoints import (
    status,
    gpu,
    monitor,
    report,
)
from .model_endpoints import (
    profiles,
    available_models,
)
from .server_endpoints import (
    stop_server,
    preflight,
    vram_devices,
    start_server,
    restart_server,
    set_keepalive,
    switch,
)
from .sampling import (
    SAMPLING_CONFIG_PATH,
    SAMPLING_DEFAULTS,
    SamplingConfigRequest,
    get_sampling_config,
    save_sampling_config,
)

__all__ = [
    "router",
    "SwitchRequest",
    "KeepaliveRequest",
    "ConfigRequest",
    "_apply_config_update",
    "check_tool_calling",
    "_reasoning_caps",
    "_config_for_profile",
    "_standalone_profile",
    "get_config",
    "set_config",
    "status",
    "gpu",
    "monitor",
    "report",
    "profiles",
    "available_models",
    "stop_server",
    "preflight",
    "vram_devices",
    "start_server",
    "restart_server",
    "set_keepalive",
    "switch",
    "SAMPLING_CONFIG_PATH",
    "SAMPLING_DEFAULTS",
    "SamplingConfigRequest",
    "get_sampling_config",
    "save_sampling_config",
]
