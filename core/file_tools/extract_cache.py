"""A short-lived, per-user, in-memory cache of whole-document text.

Paging through an uploaded PDF / Office file with read_file_chunk used to extract the entire document again for every
chunk (pdfplumber text + tables on up to 100 pages: seconds per call). The first call now extracts once and the next
chunks slice the same text.

Privacy: memory only (nothing is written to disk), keyed by user + file + mtime + size so one user never gets another's
text and an edited file is re-read, expires after TTL_S and is bounded in count and size. It holds exactly the text
the tool would return anyway; the caller applies the same masking as before.
"""
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Callable

TTL_S = 900
MAX_ENTRIES = 12
MAX_TOTAL_CHARS = 12_000_000

_lock = threading.Lock()
_items: "OrderedDict[tuple, tuple]" = OrderedDict()     # key -> (expires_at, text)


def _user() -> object:
    try:
        from ..request_context import get_current_user_id
        return get_current_user_id()
    except Exception:
        return None


def _key(path: Path) -> tuple:
    st = Path(path).stat()
    return (_user(), str(Path(path).resolve()), st.st_mtime_ns, st.st_size)


def _evict(now: float) -> None:
    for k in [k for k, (exp, _t) in _items.items() if exp <= now]:
        _items.pop(k, None)
    while _items and (len(_items) > MAX_ENTRIES or sum(len(t) for _e, t in _items.values()) > MAX_TOTAL_CHARS):
        _items.popitem(last=False)


def cached_text(path, extract: Callable[[Path], str]) -> str:
    """The whole text of `path`, extracted at most once per (user, file version) within the TTL. Blocking: call it
    from a worker thread (asyncio.to_thread)."""
    p = Path(path)
    try:
        key = _key(p)
    except OSError:
        return extract(p)
    now = time.time()
    with _lock:
        hit = _items.get(key)
        if hit and hit[0] > now:
            _items.move_to_end(key)
            return hit[1]
    text = extract(p)
    with _lock:
        _items[key] = (time.time() + TTL_S, text)
        _evict(time.time())
    return text


def forget(path) -> None:
    """Drop every cached version of this file (call when the file is deleted or replaced)."""
    target = str(Path(path).resolve())
    with _lock:
        for k in [k for k in _items if k[1] == target]:
            _items.pop(k, None)


def clear() -> None:
    with _lock:
        _items.clear()
