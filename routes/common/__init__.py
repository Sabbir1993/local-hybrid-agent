"""routes/common.py - Shared state and LLM streaming utilities across routes.
"""

from pathlib import Path
from typing import Optional

# Global CLI runtime overrides
initial_profile_path: Optional[Path] = None
models_dir: Optional[Path] = None
curStatus_model_hint: Optional[str] = None

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
    "curStatus_model_hint",
]
