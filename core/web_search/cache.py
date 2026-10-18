import hashlib
import json
import sqlite3
import time
from collections import OrderedDict
from .constants import CACHE_TTL_S, CACHE_MAX, cfg

_cache: "OrderedDict[tuple, tuple]" = OrderedDict()

# Search results (not page bodies) also survive a restart in config/web_cache.sqlite for
# web.cache_ttl_s (default 1800). Public result lists only: queries are PII-redacted and
# PAN-checked before they get here, and card numbers in snippets/passages are masked on write.
PATH = None             # set lazily to CONFIG_DIR / "web_cache.sqlite"; tests point it at a temp file
PERSIST_MAX = 2000


def _db():
    global PATH
    if PATH is None:
        from ..config import CONFIG_DIR
        PATH = CONFIG_DIR / "web_cache.sqlite"
    con = sqlite3.connect(str(PATH), timeout=5)
    con.execute("CREATE TABLE IF NOT EXISTS search_cache (k TEXT PRIMARY KEY, ts REAL, v TEXT)")
    return con


def _pkey(key: tuple) -> str:
    return hashlib.sha256(repr(key).encode("utf-8")).hexdigest()


def _persistent(key: tuple) -> bool:
    return bool(key) and key[0] == "search" and bool(cfg().get("persist_cache", True))


def cache_get(key: tuple):
    hit = _cache.get(key)
    if hit:
        ts, val = hit
        if time.time() - ts > CACHE_TTL_S:
            _cache.pop(key, None)
        else:
            _cache.move_to_end(key)
            return val
    if not _persistent(key):
        return None
    try:
        con = _db()
        try:
            row = con.execute("SELECT ts, v FROM search_cache WHERE k=?", (_pkey(key),)).fetchone()
        finally:
            con.close()
        if row and time.time() - row[0] <= float(cfg().get("cache_ttl_s", 1800)):
            val = json.loads(row[1])
            cache_put_memory(key, val)
            return val
    except Exception:
        pass                            # persistence is best-effort
    return None


def cache_put_memory(key: tuple, val) -> None:
    _cache[key] = (time.time(), val)
    _cache.move_to_end(key)
    while len(_cache) > CACHE_MAX:
        _cache.popitem(last=False)


def cache_put(key: tuple, val) -> None:
    cache_put_memory(key, val)
    if not _persistent(key):
        return
    try:
        from .. import pan
        text = json.dumps(val, ensure_ascii=False)
        text, _ = pan.mask_pans(text)
        con = _db()
        try:
            con.execute("INSERT OR REPLACE INTO search_cache (k, ts, v) VALUES (?,?,?)",
                        (_pkey(key), time.time(), text))
            con.execute("DELETE FROM search_cache WHERE ts < ?", (time.time() - 86400,))
            con.execute("DELETE FROM search_cache WHERE k NOT IN "
                        "(SELECT k FROM search_cache ORDER BY ts DESC LIMIT ?)", (PERSIST_MAX,))
            con.commit()
        finally:
            con.close()
    except Exception:
        pass
