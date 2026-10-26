"""Make the cards of a ```products block honest: every link and picture comes from a real search result.

Models rarely copy a long address exactly (they shorten it, "improve" a file name, or invent a plausible one), and a
card with a made-up page or picture is worse than a card without one. So after the answer is complete each card is
matched to the web results of this user (what web_search returned: address, title, page picture, price) and its url /
image are replaced by the real ones. A card that matches nothing keeps its text but loses its link and picture.
Per user, in memory, bounded; same lifetime as core/web_search/citations.py.
"""

import json
import re
import threading
import time
from collections import OrderedDict
from difflib import SequenceMatcher
from typing import Optional
from urllib.parse import urlsplit

_MAX = 200
_TTL_S = 3600
_lock = threading.Lock()
_results: dict = {}          # user id -> OrderedDict[url -> {title, image, price, currency, at}]
_BLOCK_RX = re.compile(r"```products[ \t]*\r?\n(.*?)```", re.S)


def note_result(uid: Optional[int], url: str, title: str = "", meta: Optional[dict] = None) -> None:
    """A web tool just returned this page for this user."""
    if not url:
        return
    meta = meta or {}
    with _lock:
        bucket = _results.setdefault(uid, OrderedDict())
        bucket.pop(url, None)
        bucket[url] = {"title": title or meta.get("name", ""), "image": meta.get("image", ""),
                       "price": meta.get("price", ""), "currency": meta.get("currency", ""), "at": time.time()}
        while len(bucket) > _MAX:
            bucket.popitem(last=False)


def reset() -> None:
    with _lock:
        _results.clear()


def _tokens(s: str) -> set:
    return {t for t in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(t) > 1}


def _loose_json(text: str):
    t = (text or "").replace("“", '"').replace("”", '"').strip()
    a, b = t.find("["), t.rfind("]")
    for cand in ([t] + ([t[a:b + 1]] if 0 <= a < b else [])):
        for s in (cand, re.sub(r",\s*([\]}])", r"\1", cand)):
            try:
                return json.loads(s)
            except ValueError:
                continue
    return None


def _score(item: dict, url: str, rec: dict) -> float:
    u = urlsplit(url)
    iu = urlsplit(str(item.get("url") or ""))
    best = 0.0
    if iu.netloc:
        best = SequenceMatcher(None, (u.netloc + u.path).lower().rstrip("/"),
                               (iu.netloc + iu.path).lower().rstrip("/")).ratio()
        if u.netloc.lower() == iu.netloc.lower() and u.path.rstrip("/") == iu.path.rstrip("/"):
            return 1.0
    a, b = _tokens(item.get("title", "")), _tokens(rec.get("title", ""))
    if a and b:
        best = max(best, len(a & b) / len(a | b) * 1.2)
    store = re.sub(r"[^a-z0-9]", "", str(item.get("store") or "").lower())
    if store and store in re.sub(r"[^a-z0-9]", "", u.netloc.lower()):
        best += 0.2
    return best


def ground_items(uid: Optional[int], items: list) -> list:
    with _lock:
        pool = list((_results.get(uid) or {}).items())
    now = time.time()
    pool = [(u, r) for u, r in pool if now - r["at"] < _TTL_S]
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        it = dict(it)
        scored = sorted(((_score(it, u, r), r.get("image") != "", u, r) for u, r in pool), key=lambda x: (x[0], x[1]),
                        reverse=True)
        if scored and scored[0][0] >= 0.6:
            _, _, url, rec = scored[0]
            it["url"] = url
            it["image"] = rec.get("image", "")
            if not it.get("price") and rec.get("price"):
                it["price"] = f"{rec['price']} {rec.get('currency', '')}".strip()
            if not it.get("store"):
                it["store"] = urlsplit(url).netloc.replace("www.", "")
        else:
            it["url"] = ""
            it["image"] = ""
        out.append(it)
    return out


def ground_text(uid: Optional[int], text: str) -> tuple:
    """-> (text, changed). Rewrites every ```products block in an answer; a block that is not JSON is left alone."""
    changed = False

    def fix(m):
        nonlocal changed
        data = _loose_json(m.group(1))
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            data = data["items"]
        if not isinstance(data, list):
            return m.group(0)
        changed = True
        return "```products\n" + json.dumps(ground_items(uid, data), ensure_ascii=False) + "\n```"

    return _BLOCK_RX.sub(fix, text or ""), changed
