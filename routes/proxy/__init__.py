"""routes/proxy - OpenAI-compatible reverse proxy forwarding to the active llama-server."""

from .cloud_proxy import _proxy_cloud, _proxy_cloud_inner
from .constants import _ALLOWED_PATHS, _CHAT_PATHS, _STRIP_HEADERS, _upstream_headers
from .endpoints import proxy, router
from .guard import (
    _SSERedactor,
    _audit_redaction,
    _guarded_json_response,
    _prompt_texts,
    _redact_json,
)

__all__ = [
    "router",
    # constants
    "_CHAT_PATHS", "_ALLOWED_PATHS", "_STRIP_HEADERS", "_upstream_headers",
    # guard
    "_prompt_texts", "_redact_json", "_audit_redaction", "_guarded_json_response", "_SSERedactor",
    # cloud proxy
    "_proxy_cloud", "_proxy_cloud_inner",
    # endpoint
    "proxy",
]
