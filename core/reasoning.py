"""
core/reasoning.py - Reasoning-effort levels (None/Low/Medium/High/Extra).

The browser sends only a level name; this module maps it to upstream request
fields, so nothing client-supplied is forwarded raw:
  - local llama-server: chat_template_kwargs.enable_thinking + a per-request
    thinking_budget_tokens (enforced by llama-server's sampler) + reasoning_effort
    for templates that read it;
  - cloud: OpenRouter `reasoning: {effort}` or OpenAI-style `reasoning_effort`.

Capability per model ("levels" | "toggle" | "none") comes from the GGUF chat
template, overridable with `reasoning` in config/model_configs.json (local) or
in the model entry of config/providers/user_<id>.json (cloud).
"""

import threading
from pathlib import Path
from typing import Optional

from .small_model import APP_CONFIG

LEVELS = ("none", "low", "medium", "high", "extra")
MODES = ("levels", "toggle", "none")
_DEFAULT_BUDGETS = {"low": 1024, "medium": 4096, "high": 12288, "extra": -1}

_mode_cache: dict = {}   # model path -> ((mtime_ns, size), mode)
_mode_lock = threading.Lock()


def _cfg() -> dict:
    return APP_CONFIG.get("reasoning_effort") or {}


def budget(level: str) -> int:
    """Thinking-token budget for a level (-1 = unrestricted, 0 = no thinking)."""
    if level == "none":
        return 0
    b = (_cfg().get("budgets") or {}).get(level, _DEFAULT_BUDGETS.get(level, -1))
    try:
        return int(b)
    except (TypeError, ValueError):
        return _DEFAULT_BUDGETS.get(level, -1)


def resolve(level: Optional[str], deep: bool = False) -> Optional[str]:
    """Effective level. An explicit level always wins (Deep research and effort
    are independent switches in the composer); None (older clients that don't
    send the field) keeps the old behaviour: Deep turns thinking on."""
    if level not in LEVELS:
        return "medium" if deep else None
    return level


def mode_from_template(tpl: str) -> str:
    if not tpl:
        return "none"
    if "enable_thinking" in tpl or "reasoning_effort" in tpl:
        return "levels"
    if "<think>" in tpl or "reasoning_content" in tpl:
        return "toggle"
    return "none"


def local_mode(model_path) -> str:
    """Capability of a local GGUF: manual override first, then its template."""
    if not model_path:
        return "none"
    p = Path(model_path)
    try:
        from .profiles import load_model_configs, _model_key
        forced = (load_model_configs().get(_model_key(p)) or {}).get("reasoning")
        if forced in MODES:
            return forced
    except Exception:
        pass
    try:
        st = p.stat()
    except OSError:
        return "none"
    sig = (st.st_mtime_ns, st.st_size)
    with _mode_lock:
        hit = _mode_cache.get(str(p))
        if hit and hit[0] == sig:
            return hit[1]
    from .vram import read_gguf_scalars
    tpl = read_gguf_scalars(p, ["tokenizer.chat_template"]).get("tokenizer.chat_template", "")
    mode = mode_from_template(tpl if isinstance(tpl, str) else "")
    with _mode_lock:
        _mode_cache[str(p)] = (sig, mode)
    return mode


def cloud_mode(cm) -> str:
    forced = (getattr(cm, "model_cfg", None) or {}).get("reasoning")
    return forced if forced in MODES else "levels"


def _template_effort(level: str) -> str:
    return "high" if level == "extra" else level


def local_fields(level: Optional[str]) -> dict:
    """llama-server request fields for an effective level ({} = template default)."""
    if level not in LEVELS:
        return {}
    if level == "none":
        # budget 0 also ends the think block at once on always-thinking models
        return {"chat_template_kwargs": {"enable_thinking": False},
                "thinking_budget_tokens": 0}
    fields = {"chat_template_kwargs": {"enable_thinking": True},
              "reasoning_effort": _template_effort(level)}
    b = budget(level)
    if b >= 0:
        fields["thinking_budget_tokens"] = b
    return fields


def cloud_fields(level: Optional[str], cm) -> dict:
    """Cloud request fields; {} for models tagged "none" or no level."""
    if level not in LEVELS or cm is None or cloud_mode(cm) == "none":
        return {}
    if "openrouter" in (getattr(cm, "base_url", "") or "").lower():
        if level == "none":
            return {"reasoning": {"enabled": False}}
        return {"reasoning": {"effort": _template_effort(level)}}
    if level == "none":
        return {"reasoning_effort": "none"}
    return {"reasoning_effort": _template_effort(level)}


# Keys cloud_fields() may add; stripped on a provider rejection before retrying
CLOUD_KEYS = ("reasoning", "reasoning_effort")
