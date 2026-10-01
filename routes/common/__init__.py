"""routes/common.py - Shared state and LLM streaming utilities across routes.
"""

from pathlib import Path
from typing import Dict, Optional

# Global CLI runtime overrides
initial_profile_path: Optional[Path] = None
models_dir: Optional[Path] = None
# Per-user model-selection hint (who answers you). This used to be a single
# process-global string, so one user's cloud switch changed the Settings
# drawer fallback and status display for everyone else on the box.
_model_hints: Dict[int, str] = {}


def get_model_hint(user_id: Optional[int]) -> Optional[str]:
    """This user's dropdown-selected model, or None if they never picked one."""
    if user_id is None:
        return None
    return _model_hints.get(int(user_id))


def set_model_hint(user_id: Optional[int], value: Optional[str]) -> None:
    """Record this user's selection without touching anyone else's."""
    if user_id is None:
        return
    if value:
        _model_hints[int(user_id)] = value
    else:
        _model_hints.pop(int(user_id), None)

from .globals import (
    current_date_prompt,
    main_ctx_tokens,
)
from .think_splitter import (
    _THINK_OPEN,
    _THINK_CLOSE,
    ThinkSplitter,
    _emit_split,
)
from .sse_stream import (
    _process_sse_stream,
)
from .fallback import (
    _llm_chat_stream_with_fallback,
)
from .overflow import (
    _is_context_overflow,
    _recover_context,
    _ctx_from_error,
    _emergency_compact,
)
from .llm_stream import (
    _main_slot_fields,
    _Admission,
    admission,
    _llm_chat_stream,
    _llm_chat_stream_raw,
)

__all__ = [
    "current_date_prompt",
    "main_ctx_tokens",
    "_THINK_OPEN",
    "_THINK_CLOSE",
    "ThinkSplitter",
    "_emit_split",
    "_process_sse_stream",
    "_llm_chat_stream_with_fallback",
    "_is_context_overflow",
    "_recover_context",
    "_ctx_from_error",
    "_emergency_compact",
    "_main_slot_fields",
    "_Admission",
    "admission",
    "_llm_chat_stream",
    "_llm_chat_stream_raw",
    "initial_profile_path",
    "models_dir",
    "get_model_hint",
    "set_model_hint",
]
