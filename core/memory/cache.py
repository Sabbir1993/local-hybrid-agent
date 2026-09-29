import sys
import threading

from .constants import _decode_vec
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


def _cached_entries() -> dict:
    gen = _chunks_gen()
    cache = _cache
    if gen is not None and cache.get("gen") == gen:
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
    built = {"gen": gen, "entries": entries, "lower": [e[2].lower() for e in entries],
             "dim": None, "mat": None, "pos": None}
    if _np is not None:
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
