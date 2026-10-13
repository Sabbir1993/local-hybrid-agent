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


def short_model_name(path_or_name) -> str:
    r"""'E:\AI\Models\Qwen3.6-35B-A3B\Qwen3.6-35B-A3B-UD-Q4_K_M.gguf' -> 'Qwen3.6-35B-A3B-UD-Q4_K_M' (what the app's
    own reports show). A value that is already a plain name comes back unchanged."""
    name = str(path_or_name or "").replace("\\", "/").rstrip("/").split("/")[-1]
    return name[:-5] if name.lower().endswith(".gguf") else name


def short_models_listing(data: dict) -> dict:
    """llama-server lists its model by absolute file path (in both the Ollama-style `models` and the OpenAI-style `data`
    arrays). An API client should see a plain model name, and not the server's folder layout."""
    if not isinstance(data, dict):
        return data
    for m in data.get("models") or []:
        if isinstance(m, dict):
            for k in ("name", "model"):
                if m.get(k):
                    m[k] = short_model_name(m[k])
    for d in data.get("data") or []:
        if isinstance(d, dict) and d.get("id"):
            d["id"] = short_model_name(d["id"])
            d["aliases"] = [d["id"]]
    return data
