"""routes/chat.py - Fast chat endpoint with real-time web search and streaming tool use.
"""

from .base import (
    router,
)
from .models import (
    ChatRunRequest,
    CompactRequest,
)
from .delivery import (
    _saved_filename,
    _prompt_tokens_of,
    _HELPER_EXTS,
    _requested_exts,
    _pick_deliverables,
    _finalize_download_tags,
    _finalize_media,
    _wraps_media,
    _WRAP_REFUSAL,
)
from .file_intent import (
    WEB_TOOL_NAMES,
    _FILE_VERB_RE,
    _FILE_NOUN_RE,
    _ANNOUNCE_RE,
    _DL_TAG_RE,
    _FILE_REF_RE,
    _FILE_WHERE_RE,
    _FILE_EDIT_RE,
    _TEXT_FILE_EXTS,
    PRIOR_FILE_MAX_CHARS,
    _ATTACHED_DOC_RE,
    _DOC_EDIT_EXTS,
    session_files,
    file_followup_intent,
    load_prior_file,
    wants_file_output,
    looks_undelivered,
    shrink_old_tool_results,
)
from .run import (
    chat_run,
)
from .compact import (
    _COMPACT_SYSTEM_PROMPT,
    _strip_think,
    _summarize_history,
    chat_compact,
)

__all__ = [
    "router",
    "ChatRunRequest",
    "CompactRequest",
    "_saved_filename",
    "_prompt_tokens_of",
    "_HELPER_EXTS",
    "_requested_exts",
    "_pick_deliverables",
    "_finalize_download_tags",
    "_finalize_media",
    "_wraps_media",
    "_WRAP_REFUSAL",
    "WEB_TOOL_NAMES",
    "_FILE_VERB_RE",
    "_FILE_NOUN_RE",
    "_ANNOUNCE_RE",
    "_DL_TAG_RE",
    "_FILE_REF_RE",
    "_FILE_WHERE_RE",
    "_FILE_EDIT_RE",
    "_TEXT_FILE_EXTS",
    "PRIOR_FILE_MAX_CHARS",
    "_ATTACHED_DOC_RE",
    "_DOC_EDIT_EXTS",
    "session_files",
    "file_followup_intent",
    "load_prior_file",
    "wants_file_output",
    "looks_undelivered",
    "shrink_old_tool_results",
    "chat_run",
    "_COMPACT_SYSTEM_PROMPT",
    "_strip_think",
    "_summarize_history",
    "chat_compact",
]
