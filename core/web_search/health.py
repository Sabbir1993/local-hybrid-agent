"""Per-backend circuit breaker: an engine that keeps failing (CAPTCHA, 403, down) is skipped for a
growing cool-down instead of costing every search its full timeout. In-memory, per process."""
import threading
import time

FAIL_LIMIT = 3          # consecutive failures before the breaker opens
BASE_COOLDOWN_S = 60
MAX_COOLDOWN_S = 600

_lock = threading.Lock()
_state: dict = {}       # backend -> {"fails": int, "until": float}


def is_open(name: str) -> bool:
    """True while the backend is cooling down (skip it)."""
    with _lock:
        e = _state.get(name)
        return bool(e and e["until"] > time.time())


def record(name: str, ok: bool) -> None:
    with _lock:
        if ok:
            _state.pop(name, None)
            return
        e = _state.setdefault(name, {"fails": 0, "until": 0.0})
        e["fails"] += 1
        if e["fails"] >= FAIL_LIMIT:
            over = e["fails"] - FAIL_LIMIT
            e["until"] = time.time() + min(BASE_COOLDOWN_S * (2 ** over), MAX_COOLDOWN_S)


def snapshot() -> dict:
    now = time.time()
    with _lock:
        return {k: {"fails": v["fails"], "cooldown_s": max(0, int(v["until"] - now))} for k, v in _state.items()}


def reset() -> None:
    with _lock:
        _state.clear()


_next_slot: dict = {}   # key -> earliest monotonic time the next call may start


def wait_turn(key: str, min_interval_s: float) -> None:
    """Blocking (call from a worker thread): spaces calls under `key` at least min_interval_s apart,
    so parallel searches don't hammer one scraped engine or one host."""
    if min_interval_s <= 0:
        return
    with _lock:
        now = time.monotonic()
        slot = max(now, _next_slot.get(key, 0.0))
        _next_slot[key] = slot + min_interval_s
        if len(_next_slot) > 512:
            for k in [k for k, v in _next_slot.items() if v < now]:
                _next_slot.pop(k, None)
    delay = slot - now
    if delay > 0:
        time.sleep(min(delay, 10.0))
