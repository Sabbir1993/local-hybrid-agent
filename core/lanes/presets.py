from contextvars import ContextVar
from typing import Optional


AGENT_MODES = (
    "all-local",
    "main-local-rest-cloud",
    "main-cloud-rest-local",
    "no-orchestration",
)
_RUNTIME_MODES = AGENT_MODES + ("all-cloud",)

_LEGACY_MODES = {
    "main": "all-local",
    "tiered": "main-cloud-rest-local",
    "agent": "all-local",
    "direct": "no-orchestration",
}

request_agent_mode: ContextVar[Optional[str]] = ContextVar("request_agent_mode", default=None)


def normalize_agent_mode(value, fallback: Optional[str] = "all-local") -> Optional[str]:
    mode = str(value or "").strip().lower()
    mode = _LEGACY_MODES.get(mode, mode)
    if mode in _RUNTIME_MODES:
        return mode
    return fallback


def stored_agent_mode(user_id: Optional[int] = None) -> Optional[str]:
    from ..cloud.config import _merged

    mode = normalize_agent_mode(_merged(user_id).get("agent_mode"), None)
    return mode if mode in AGENT_MODES else None


def agent_mode(user_id: Optional[int] = None, fallback: str = "all-local") -> str:
    return stored_agent_mode(user_id) or normalize_agent_mode(request_agent_mode.get(), None) \
        or normalize_agent_mode(fallback) or "all-local"


def set_request_agent_mode(mode: str) -> str:
    canonical = normalize_agent_mode(mode)
    request_agent_mode.set(canonical)
    return canonical


def set_agent_mode(user_id: int, mode: str) -> str:
    canonical = normalize_agent_mode(mode, None)
    if canonical not in AGENT_MODES:
        raise ValueError(f"unknown agent mode '{mode}'")
    from .. import cloud

    cloud.write_user_section(user_id, "agent_mode", canonical)
    return canonical
