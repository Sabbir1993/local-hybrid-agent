import re
import sys
import time
from typing import Optional
from .constants import _CACHE_TTL_S, _NULL_WORDS

# Compiled-pattern cache: (pattern, flags) -> compiled regex | None (invalid).
_compile_cache: dict[tuple, Optional[re.Pattern]] = {}
_cache_ts: float = 0.0


def _get_cache_ts() -> float:
    mod = sys.modules.get("core.input_guard")
    return getattr(mod, "_cache_ts", _cache_ts)


def _set_cache_ts(val: float) -> None:
    global _cache_ts
    _cache_ts = val
    mod = sys.modules.get("core.input_guard")
    if mod is not None:
        setattr(mod, "_cache_ts", val)


def _get_compile_cache() -> dict:
    mod = sys.modules.get("core.input_guard")
    return getattr(mod, "_compile_cache", _compile_cache)


def _compile(pattern: str) -> Optional[re.Pattern]:
    """Cached regex compile; invalid patterns return None (rule skipped)."""
    now = time.time()
    cache_ts = _get_cache_ts()
    cache = _get_compile_cache()
    if now - cache_ts > _CACHE_TTL_S:
        cache.clear()      # pick up config edits
        _set_cache_ts(now)
    key = (pattern, re.IGNORECASE)
    if key not in cache:
        try:
            cache[key] = re.compile(pattern, re.IGNORECASE)
        except re.error:
            cache[key] = None
    return cache[key]


def _clean_list(values) -> list:
    """Null-tolerant list coercion: 'null'/'all'/'none'/'' mean NO restriction
    (the admin left the filter unset) and are dropped."""
    return [str(v).strip() for v in (values or [])
            if str(v).strip() and str(v).strip().lower() not in _NULL_WORDS]


def _rule_applies_to(rule: dict, user) -> bool:
    """Point #3: role/user targeting. Empty lists apply to everyone."""
    roles = _clean_list(rule.get("roles"))
    users = _clean_list(rule.get("users"))
    if not roles and not users:
        return True
    if users:
        uname = getattr(user, "username", None)
        if uname and str(uname) in {str(u) for u in users}:
            return True
    if roles:
        user_roles = {str(r) for r in (getattr(user, "role_names", None) or [])}
        if user_roles and user_roles & {str(r) for r in roles}:
            return True
    return False


def is_semantic(rule: dict) -> bool:
    return str(rule.get("type") or "regex").strip().lower() == "semantic"
