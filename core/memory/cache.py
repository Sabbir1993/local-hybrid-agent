import sys
import threading
import time

from .constants import _decode_vec, _knowledge_id_from_path
from .store import _db

try:
    import numpy as _np
except Exception:
    _np = None

_cache_lock = threading.Lock()
_cache: dict = {"gen": None}


def _chunks_gen():
    row = _db().execute("SELECT value FROM meta WHERE key = 'chunks_gen'").fetchone()
    return row[0] if row else None


STALE_WHILE_INGESTING_S = 60.0       # during a bulk ingest, rebuild the whole search cache at most this often


def _ingest_active() -> bool:
    try:
        from .. import kb_queue
        return kb_queue.active()
    except Exception:
        return False


def _cached_entries() -> dict:
    gen = _chunks_gen()
    cache = _cache
    if gen is not None and cache.get("gen") == gen:
        return cache
    # Every finished document bumps the generation; reloading hundreds of thousands of chunks after each one
    # would make searches during a bulk upload wait many seconds. Serve the previous cache for a short while.
    if cache.get("entries") and _ingest_active() and time.time() - cache.get("built_at", 0) < STALE_WHILE_INGESTING_S:
        return cache
    with _cache_lock:
        gen = _chunks_gen()
        if gen is not None and _cache.get("gen") == gen:
            return _cache
        rows = _db().execute("SELECT source, path, text, vec FROM chunks ORDER BY id").fetchall()
        built = _build_cache([(source, path, text, _decode_vec(vec)) for source, path, text, vec in rows], gen)
        _set_cache(built)
        return built


def _build_cache(entries: list, gen=None) -> dict:
    """entries: [(source, path, text, vec_or_None)] -> the search cache."""
    built = {"gen": gen, "built_at": time.time(), "entries": entries, "lower": [e[2].lower() for e in entries],
             "dim": None, "mat": None, "pos": None, "kids": None, "wc": {}}
    if _np is not None:
        # knowledge source id per row (-1 = not a knowledge row): the access filter becomes one np.isin
        kids = []
        for e in entries:
            kid = _knowledge_id_from_path(e[1]) if e[0] == "knowledge" else None
            kids.append(-1 if kid is None else kid)
        built["kids"] = _np.asarray(kids, dtype=_np.int64)
        dims = [len(e[3]) for e in entries if e[3] is not None]
        if dims:
            dim = max(set(dims), key=dims.count)
            keep = [i for i, e in enumerate(entries) if e[3] is not None and len(e[3]) == dim]
            mat = _np.vstack([_np.asarray(entries[i][3], dtype=_np.float32) for i in keep])
            norms = _np.linalg.norm(mat, axis=1)
            mat = (mat / _np.maximum(norms, 1e-9)[:, None]).astype(_np.float32)
            mat[norms <= 1e-9] = 0.0
            pos = _np.full(len(entries), -1, dtype=_np.int64)
            pos[keep] = _np.arange(len(keep))
            built.update(dim=dim, mat=mat, pos=pos)
    return built


_TERM_CACHE_MAX = 32          # query words whose per-row counts are kept (int32 per row each)


def _term_counts(cache: dict, word: str):
    """Occurrences of `word` in every cached row (np.int32, same order as entries). Counted once per word per
    cache generation: repeat queries (and follow-ups sharing words) skip the O(rows) str.count pass."""
    wc = cache["wc"]
    arr = wc.get(word)
    if arr is None:
        lower = cache["lower"]
        arr = _np.fromiter((tl.count(word) for tl in lower), dtype=_np.int32, count=len(lower))
        if len(wc) >= _TERM_CACHE_MAX:
            wc.pop(next(iter(wc)), None)
        wc[word] = arr
    return arr


def _lex_counts(cache: dict, words: list, idxs: list) -> list:
    """Same value as float(sum(row.count(w) for w in words)) for each row in idxs."""
    ix = _np.asarray(idxs, dtype=_np.int64)
    total = _np.zeros(len(ix), dtype=_np.float64)
    for w in words:
        total += _term_counts(cache, w)[ix]
    return total.tolist()


def _set_cache(built: dict) -> None:
    global _cache
    _cache = built


def _load_entries() -> list:
    pkg = sys.modules.get("core.memory")
    cached_fn = getattr(pkg, "_cached_entries", _cached_entries)
    return list(cached_fn()["entries"])


def _cosines(cache: dict, idxs: list, qvec) -> list:
    """Cosine of the query against cached rows idxs (0.0 for rows with no vector)."""
    if qvec is None or not idxs:
        return [0.0] * len(idxs)
    if _np is not None and cache.get("mat") is not None:
        q = _np.asarray(qvec, dtype=_np.float32)
        qn = float(_np.linalg.norm(q))
        if len(q) != cache["dim"] or qn < 1e-9:
            return [0.0] * len(idxs)
        pos = cache["pos"][_np.asarray(idxs, dtype=_np.int64)]
        out = _np.zeros(len(idxs), dtype=_np.float32)
        has = pos >= 0
        if has.any():
            out[has] = cache["mat"][pos[has]] @ (q / qn)
        return out.tolist()
    out = []
    for i in idxs:
        v = cache["entries"][i][3]
        if v is None or len(v) != len(qvec):
            out.append(0.0)
            continue
        dot = sum(x * y for x, y in zip(qvec, v))
        na = sum(x * x for x in qvec) ** 0.5
        nb = sum(y * y for y in v) ** 0.5
        out.append(dot / (na * nb) if na * nb > 1e-9 else 0.0)
    return out
