import re
import sys
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse
from .constants import _STOP, _TRACKING


def norm_url(u: str) -> str:
    p = urlparse(u)
    host = (p.netloc or "").lower()
    host = host[4:] if host.startswith("www.") else host
    q = urlencode([(k, v) for k, v in parse_qsl(p.query) if not _TRACKING.match(k)])
    return f"{host}{p.path.rstrip('/') or '/'}" + (f"?{q}" if q else "")


def merge(ranked_lists: dict) -> list:
    """Reciprocal-rank fusion across engines; a URL found by several engines ranks higher."""
    pool: dict = {}
    for engine, results in ranked_lists.items():
        for rank, r in enumerate(results):
            key = norm_url(r["url"])
            e = pool.get(key)
            if e is None:
                e = pool[key] = {**r, "engines": [], "rrf": 0.0}
            elif len(r.get("snippet") or "") > len(e.get("snippet") or ""):
                e["snippet"] = r["snippet"]
            e["engines"].append(engine)
            e["rrf"] += 1.0 / (60 + rank)
    return sorted(pool.values(), key=lambda x: -x["rrf"])


def terms(text: str) -> set:
    return {t for t in re.findall(r"\w+", (text or "").lower()) if len(t) > 1 and t not in _STOP}


def lexical(query_terms: set, text: str) -> float:
    if not query_terms:
        return 0.0
    return len(query_terms & terms(text)) / len(query_terms)


def _minmax(vals: list) -> list:
    if not vals:
        return []
    lo, hi = min(vals), max(vals)
    return [0.0] * len(vals) if hi - lo < 1e-9 else [(v - lo) / (hi - lo) for v in vals]


async def embed(texts: list) -> Optional[list]:
    try:
        from ..memory import _embed_texts
        return await _embed_texts(texts)
    except Exception:
        return None


def _cosines(qv, vecs) -> list:
    import numpy as np
    m = np.asarray(vecs, dtype=np.float32)
    q = np.asarray(qv, dtype=np.float32)
    denom = np.linalg.norm(m, axis=1) * (np.linalg.norm(q) or 1.0)
    denom[denom < 1e-9] = 1.0
    return (m @ q / denom).tolist()


async def rerank(query: str, items: list) -> list:
    if len(items) < 2:
        return items
    qt = terms(query)
    lex = [lexical(qt, f"{i['title']} {i['snippet']}") for i in items]
    rrf = _minmax([i["rrf"] for i in items])
    embed_fn = getattr(sys.modules.get("core.web_search"), "embed", embed)
    vecs = await embed_fn([f"search_query: {query}"] +
                          [f"search_document: {i['title']}. {i['snippet']}" for i in items])
    if vecs:
        cos = _minmax(_cosines(vecs[0], vecs[1:]))
        scores = [0.4 * r + 0.4 * c + 0.2 * l for r, c, l in zip(rrf, cos, lex)]
    else:
        scores = [0.6 * r + 0.4 * l for r, l in zip(rrf, lex)]
    for i, s in zip(items, scores):
        i["score"] = round(s, 4)
    return sorted(items, key=lambda x: -x["score"])
