"""Which URLs did the web tools actually return? An answer that cites [n](URL) can then be checked
against them, so a model cannot present an invented link as a source.

Per user, in memory, bounded: the last SEEN_MAX URLs for SEEN_TTL_S."""
import re
import threading
import time
from collections import OrderedDict
from typing import Optional

from .rerank import norm_url

SEEN_MAX = 300
SEEN_TTL_S = 3600
_LINK_RX = re.compile(r"\[(\d{1,2})\]\((https?://[^\s)]+)\)")

_lock = threading.Lock()
_seen: dict = {}   # user id -> OrderedDict[norm_url -> timestamp]


def remember(uid: Optional[int], urls) -> None:
    now = time.time()
    with _lock:
        bucket = _seen.setdefault(uid, OrderedDict())
        for u in urls or []:
            if u:
                bucket[norm_url(str(u))] = now
                bucket.move_to_end(norm_url(str(u)))
        while len(bucket) > SEEN_MAX:
            bucket.popitem(last=False)


def known(uid: Optional[int], url: str) -> bool:
    cutoff = time.time() - SEEN_TTL_S
    with _lock:
        ts = (_seen.get(uid) or {}).get(norm_url(url))
    return bool(ts and ts >= cutoff)


def unverified_links(uid: Optional[int], answer: str) -> list:
    """Cited [n](URL) links in `answer` that no web tool returned for this user."""
    bad = []
    for _, url in _LINK_RX.findall(answer or ""):
        if not known(uid, url) and url not in bad:
            bad.append(url)
    return bad


def reset() -> None:
    with _lock:
        _seen.clear()


def strip_unverified(uid: Optional[int], answer: str) -> tuple:
    """-> (answer, n_removed). A cited [n](URL) whose URL no web tool returned loses its link
    (the [n] stays), and a one-line note says so. Only call when the turn actually used the web."""
    bad = set(unverified_links(uid, answer))
    if not bad:
        return answer, 0
    out = _LINK_RX.sub(lambda m: f"[{m.group(1)}]" if m.group(2) in bad else m.group(0), answer)
    return out + f"\n\n_Note: {len(bad)} cited link(s) were not among the sources found by search and were removed._", len(bad)
