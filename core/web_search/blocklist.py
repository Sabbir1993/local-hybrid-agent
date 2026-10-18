"""Remembers sites that refuse automated access (401/403/429/451) so the model stops retrying them.

A host is blocked after FAIL_THRESHOLD refusals; the block expires after TTL_S (1h for 429).
State is kept in config/web_blocked.json. Config overrides (app.json "web"):
  blocked_hosts  always blocked      allow_hosts  never blocked
"""
import json
import threading
import time
from urllib.parse import urlparse

from ..config import CONFIG_DIR, atomic_write_json
from .constants import cfg

BLOCK_CODES = {401, 403, 429, 451}
FAIL_THRESHOLD = 2
TTL_S = 24 * 3600
TTL_429_S = 3600
PATH = CONFIG_DIR / "web_blocked.json"

_lock = threading.Lock()
_state = None   # host -> {"fails": int, "until": float, "status": int}


def host_of(url: str) -> str:
    h = (urlparse(url if "://" in url else "https://" + url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def _match(host: str, names) -> bool:
    return any(host == n or host.endswith("." + n) for n in (str(x).lower().lstrip(".") for x in names or []))


def _load() -> dict:
    global _state
    if _state is None:
        try:
            _state = json.loads(PATH.read_text(encoding="utf-8"))
        except Exception:
            _state = {}
    return _state


def _save() -> None:
    try:
        atomic_write_json(PATH, _state)
    except Exception:
        pass   # persistence is best-effort; the in-memory list still works


def _key(host: str) -> str:
    """en.wikipedia.org and m.wikipedia.org are one site for blocking purposes."""
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    two_level = len(parts[-1]) == 2 and parts[-2] in ("co", "com", "org", "net", "gov", "ac", "edu")
    return ".".join(parts[-3:] if two_level else parts[-2:])


def record_failure(url: str, status: int) -> None:
    if status not in BLOCK_CODES:
        return
    host = host_of(url)
    if not host or _match(host, cfg().get("allow_hosts")):
        return
    key = _key(host)
    with _lock:
        st = _load()
        e = st.get(key) or {"fails": 0, "until": 0, "status": status}
        if e.get("until", 0) and e["until"] < time.time():
            e = {"fails": 0, "until": 0, "status": status}   # expired: start counting again
        e["fails"] += 1
        e["status"] = status
        if e["fails"] >= FAIL_THRESHOLD or status == 429:
            e["until"] = time.time() + (TTL_429_S if status == 429 else TTL_S)
        st[key] = e
        _save()


def is_blocked(url: str) -> bool:
    host = host_of(url)
    if not host or _match(host, cfg().get("allow_hosts")):
        return False
    if _match(host, cfg().get("blocked_hosts")):
        return True
    with _lock:
        e = _load().get(_key(host))
    return bool(e and e.get("until", 0) > time.time())


def status_of(url: str) -> int:
    with _lock:
        return int((_load().get(_key(host_of(url))) or {}).get("status") or 403)
