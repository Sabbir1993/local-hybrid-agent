"""routes/agent.py - Autonomous multi-turn coding agent, shell permissions, vision, and workspace tools.
"""

# shared objects the run reads (tests and callers reach them through the package)
from core.agent_loop.narration import _is_narration  # noqa: F401
from core.small_model import APP_CONFIG  # noqa: F401

from .base import (
    _gpu_device_label,
    _main_device_label,
    _executor_device_label,
    router,
)
from .models import (
    AttachedFile,
    AgentRequest,
    PermissionAnswerReq,
    RunFeedbackReq,
    CommandExpandReq,
    VisionReq,
)
from .constants import (
    AGENT_MAX_STEPS,
    LOOP_STOP_STREAK,
    MAX_PLAN_NUDGES,
    ESC_STREAK_LIMIT,
    DEFAULT_RUN_TIMEOUT_S,
    NEAR_REPEAT_LIMIT,
    NEAR_REPEAT_WINDOW,
    _run_timeout_s,
    PLAN_MODE_TOOLS,
    EXECUTOR_TEST_TOOLS,
    PLAN_MODE_PROMPT,
    MAX_UPLOAD_BYTES,
    MAX_UPLOAD_FILES,
)
from .guards import (
    _guard_flush_events,
    _guard_audit,
    _executor_grammar_disabled,
    _executor_grammar,
    _DOWNLOAD_MARKER_RE,
    _strip_download_markers,
    _with_diff,
)
from .permissions import (
    _perm_pending,
    agent_permission_answer,
    _pattern_error,
    _await_permission,
)
from .run import (
    agent_run,
)
from .meta_endpoints import (
    agent_project_instructions,
    agent_feedback,
    agent_commands,
    agent_command_expand,
    agent_workspace,
)
from .memory_endpoints import (
    memory_list,
    memory_get,
    memory_put,
    memory_remove,
    memory_remove_all,
)
from .upload import (
    agent_upload,
    _unique_dest,
)
from .download import (
    _resolve_requested_file,
    agent_download,
    agent_raw,
)
from .preview import (
    _PREVIEW_TTL_S,
    _PREVIEW_MAX_BYTES,
    _PREVIEW_MAX_PER_USER,
    _previews,
    PreviewHtmlReq,
    put_preview_html,
    get_preview_html,
    agent_slides,
)
from .ws_browse import (
    _ws_tree_scan,
    _ws_diff_lines,
    agent_ws_tree,
    agent_ws_file,
    _WS_RAW_TEXT_EXT,
    _WS_RAW_MAX,
    agent_ws_raw,
)
from .vision import (
    agent_vision,
    unload_small_models_endpoint,
)

__all__ = [
    "_gpu_device_label",
    "_main_device_label",
    "_executor_device_label",
    "router",
    "AttachedFile",
    "AgentRequest",
    "PermissionAnswerReq",
    "RunFeedbackReq",
    "CommandExpandReq",
    "VisionReq",
    "AGENT_MAX_STEPS",
    "LOOP_STOP_STREAK",
    "MAX_PLAN_NUDGES",
    "ESC_STREAK_LIMIT",
    "DEFAULT_RUN_TIMEOUT_S",
    "NEAR_REPEAT_LIMIT",
    "NEAR_REPEAT_WINDOW",
    "_run_timeout_s",
    "PLAN_MODE_TOOLS",
    "EXECUTOR_TEST_TOOLS",
    "PLAN_MODE_PROMPT",
    "MAX_UPLOAD_BYTES",
    "MAX_UPLOAD_FILES",
    "_guard_flush_events",
    "_guard_audit",
    "_executor_grammar_disabled",
    "_executor_grammar",
    "_DOWNLOAD_MARKER_RE",
    "_strip_download_markers",
    "_with_diff",
    "_perm_pending",
    "agent_permission_answer",
    "_pattern_error",
    "_await_permission",
    "agent_run",
    "agent_project_instructions",
    "agent_feedback",
    "agent_commands",
    "agent_command_expand",
    "agent_workspace",
    "agent_upload",
    "_unique_dest",
    "_resolve_requested_file",
    "agent_download",
    "agent_raw",
    "_PREVIEW_TTL_S",
    "_PREVIEW_MAX_BYTES",
    "_PREVIEW_MAX_PER_USER",
    "_previews",
    "PreviewHtmlReq",
    "put_preview_html",
    "get_preview_html",
    "agent_slides",
    "_ws_tree_scan",
    "_ws_diff_lines",
    "agent_ws_tree",
    "agent_ws_file",
    "_WS_RAW_TEXT_EXT",
    "_WS_RAW_MAX",
    "agent_ws_raw",
    "agent_vision",
    "unload_small_models_endpoint",
]
