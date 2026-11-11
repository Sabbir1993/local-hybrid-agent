"""What the deck builder changed on its own while turning an outline into slides: a chart it could not draw, a layout it
replaced, text it left out. Collected during one build (thread-local) and kept by the finished file's hash, so the tool
result can tell the model exactly what to correct instead of leaving it to discover a different deck than it asked for."""
import hashlib
import threading
from collections import OrderedDict

_local = threading.local()
_BY_FILE: "OrderedDict" = OrderedDict()
_LOCK = threading.Lock()
MAX_FILES = 64
MAX_NOTES = 8


def start() -> None:
    _local.items = []


def note(msg: str) -> None:
    items = getattr(_local, "items", None)
    if items is not None and msg not in items and len(items) < 40:
        items.append(msg)


def finish(data: bytes) -> None:
    """Attach the notes of the build that just ended to its output."""
    items, _local.items = list(getattr(_local, "items", None) or []), None
    if not items or not data:
        return
    with _LOCK:
        _BY_FILE[hashlib.sha256(data).hexdigest()] = items
        while len(_BY_FILE) > MAX_FILES:
            _BY_FILE.popitem(last=False)


def summary(data: bytes) -> str:
    """One sentence for the tool result ('' when the builder changed nothing)."""
    with _LOCK:
        items = _BY_FILE.get(hashlib.sha256(data).hexdigest()) or []
    if not items:
        return ""
    more = f" (+{len(items) - MAX_NOTES} more)" if len(items) > MAX_NOTES else ""
    return ("Built differently from the outline: " + "; ".join(items[:MAX_NOTES]) + more
            + ". If that matters, correct those slides and rebuild with doc_create.")
