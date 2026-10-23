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


_DAY_RX = re.compile(r"\b(today|tonight|right now|breaking|live score|this morning)\b", re.I)
_MONTH_RX = re.compile(r"\b(latest|news|recent(ly)?|current|newest|this week|this month|just (released|announced)|"
                       r"update[sd]?|rate today|price today|exchange rate)\b", re.I)


# Topics whose answers go stale within months: what is "found" or "top" now is a question about this year, unless the query
# names an exact case (a CVE id) or a year itself.
_YEAR_RX = re.compile(r"\b(vulnerabilit(?:y|ies)|exploits?|zero[- ]?days?|security (?:flaws?|issues?|advisor(?:y|ies)|patch(?:es)?)|"
                      r"ransomware|data breach(?:es)?|malware campaigns?|top \d+)\b", re.I)
_CVE_ID_RX = re.compile(r"\bCVE-\d{4}-\d{3,}\b", re.I)
_EXPLICIT_YEAR_RX = re.compile(r"(?<![\w-])(?:19|20)\d\d(?![\w-])")


def detect_recency(query: str) -> str:
    """'' for evergreen queries; day/month/year when the wording asks for fresh information."""
    if _DAY_RX.search(query or ""):
        return "day"
    if _MONTH_RX.search(query or ""):
        return "month"
    if _YEAR_RX.search(query or "") and not _CVE_ID_RX.search(query or ""):
        return "year"
    return ""


def _host_in(url: str, names) -> bool:
    host = (urlparse(url).hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    return any(host == n or host.endswith("." + n)
               for n in (str(x).lower().strip().lstrip(".") for x in names or []) if n)


def apply_domain_prefs(items: list, deny=None, boost=None, bonus: float = 0.15) -> list:
    """web.deny_hosts are dropped; web.boost_hosts (official sources: gov.bd, bb.org.bd, vendor
    docs) get `bonus` added to their rerank score, so they win ties and near-ties, not everything."""
    items = [i for i in items if not _host_in(i["url"], deny)]
    if boost:
        for i in items:
            if _host_in(i["url"], boost):
                i["score"] = round(i.get("score", 0.0) + bonus, 4)
                i["boosted"] = True
        items.sort(key=lambda x: -x.get("score", 0.0))
    return items


def dedup_items(items: list, threshold: float = 0.8) -> list:
    """Drop a result whose title+snippet is a near-copy (term Jaccard >= threshold) of a
    better-ranked one - mirrors and scrapers of the same article."""
    kept, sigs = [], []
    for i in items:
        sig = terms(f"{i.get('title', '')} {i.get('snippet', '')}")
        if sig and any(len(sig & s) / len(sig | s) >= threshold for s in sigs):
            continue
        kept.append(i)
        sigs.append(sig)
    return kept


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
