"""Constants and small helpers for the proxy router."""

from fastapi import Request

_CHAT_PATHS = ("v1/chat/completions", "chat/completions")
# Only the OpenAI-compatible inference surface is exposed. llama-server's
# admin endpoints (/slots erase/save/restore, /props, /lora-adapters,
# /metrics) would let any user wipe or read other users' KV cache.
_ALLOWED_PATHS = frozenset(_CHAT_PATHS + (
    "v1/completions", "completions", "v1/embeddings", "embeddings", "v1/models", "models", "health",
))

# Never forward the caller's app credentials to llama-server / cloud upstreams.
_STRIP_HEADERS = {"host", "cookie", "authorization", "x-csrf-token", "content-length"}


def _upstream_headers(request: Request) -> dict:
    return {k: v for k, v in request.headers.items() if k.lower() not in _STRIP_HEADERS}
