"""Administrator ("System") terminal sessions: who has switched it on, and until when.

The elevated process itself lives on the user's machine (companion/adminops.js). This module is only the server's record that a
signed-in person asked for it and confirmed on their own PC. It is in memory on purpose: a restart ends every administrator
session, nothing is stored, and the next switch-on asks again.
"""
import time
from typing import Optional

DEFAULT_IDLE_MIN = 15

_sessions: dict = {}      # user_id -> {"started": ts, "last": ts}


def idle_seconds() -> float:
    """terminal.admin_idle_min in app.json (default 15, 1..120): idle time after which the session ends."""
    try:
        from .small_model import APP_CONFIG
        cfg = APP_CONFIG.get("terminal")
        v = (cfg or {}).get("admin_idle_min", DEFAULT_IDLE_MIN) if isinstance(cfg, dict) else DEFAULT_IDLE_MIN
        return float(min(120, max(1, float(v)))) * 60.0
    except (TypeError, ValueError):
        return DEFAULT_IDLE_MIN * 60.0


def start(user_id: int) -> None:
    now = time.time()
    _sessions[user_id] = {"started": now, "last": now}


def active(user_id: Optional[int]) -> bool:
    """True while this user's administrator terminal is on. An idle session is ended here, not left to a timer."""
    s = _sessions.get(user_id)
    if not s:
        return False
    if time.time() - s["last"] > idle_seconds():
        _sessions.pop(user_id, None)
        return False
    return True


def touch(user_id: int) -> None:
    s = _sessions.get(user_id)
    if s:
        s["last"] = time.time()


def end(user_id: int) -> bool:
    return _sessions.pop(user_id, None) is not None


def remaining_seconds(user_id: int) -> int:
    s = _sessions.get(user_id)
    return max(0, int(idle_seconds() - (time.time() - s["last"]))) if s and active(user_id) else 0


def clear() -> None:
    _sessions.clear()
