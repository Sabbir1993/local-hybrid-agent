import re
import sys
from typing import Optional

from .cache import _cached_entries, _cosines
from .constants import _knowledge_id_from_path, _norm, _session_id_from_path
from .indexing import _embed_texts


def _pkg():
    return sys.modules.get("core.memory")


async def search_memory_hybrid(query: str, k: int = 8, requesting_user_id: Optional[int] = None,
                               allowed_knowledge_source_ids: Optional[set] = None,
                               sources: Optional[set] = None) -> list:
    cached_fn = getattr(_pkg(), "_cached_entries", _cached_entries)
    cache = cached_fn()
    all_entries = cache["entries"]

    from ..db import db_session_owner
    owners: dict = {}
    idxs = []
    kid_fn = getattr(_pkg(), "_knowledge_id_from_path", _knowledge_id_from_path)
    sid_fn = getattr(_pkg(), "_session_id_from_path", _session_id_from_path)

    for i, (source, path, _text, _v) in enumerate(all_entries):
        if source == "workspace" or (sources is not None and source not in sources):
            continue
        if source == "session":
            if requesting_user_id is None:
                continue
            sid = sid_fn(path)
            if sid not in owners:
                owners[sid] = db_session_owner(sid) if sid is not None else None
            if owners[sid] != requesting_user_id:
                continue
        elif source == "knowledge":
            if not allowed_knowledge_source_ids:
                continue
            kid = kid_fn(path)
            if kid is None or kid not in allowed_knowledge_source_ids:
                continue
        idxs.append(i)
    if not idxs:
        return []

    embed_fn = getattr(_pkg(), "_embed_texts", _embed_texts)
    qv = await embed_fn([query])
    qvec = qv[0] if qv else None

    words = [w.lower() for w in re.findall(r"\w{3,}", query)][:8]
    lower = cache["lower"]
    lex_raw = [float(sum(lower[i].count(w) for w in words)) for i in idxs]
    cosines_fn = getattr(_pkg(), "_cosines", _cosines)
    cos_raw = cosines_fn(cache, idxs, qvec)

    norm_fn = getattr(_pkg(), "_norm", _norm)
    lex_n = norm_fn(lex_raw)
    cos_n = norm_fn(cos_raw) if qvec is not None else None
    results = []
    for j, i in enumerate(idxs):
        source, path, text, _v = all_entries[i]
        score = (0.5 * cos_n[j] + 0.5 * lex_n[j]) if cos_n is not None else lex_n[j]
        results.append({"source": source, "path": path, "text": text, "score": score})
    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:max(1, k)]


async def search_knowledge_hybrid(query: str, k: int = 6,
                                  allowed_knowledge_source_ids: Optional[set] = None,
                                  min_cos: float = 0.45, min_lex: int = 2) -> list:
    if not allowed_knowledge_source_ids:
        return []

    source_titles = {}
    try:
        from ..auth_db import list_knowledge_sources
        for ks in list_knowledge_sources():
            if ks["id"] in allowed_knowledge_source_ids and ks["status"] == "ready":
                source_titles[ks["id"]] = ks["title"]
    except Exception as e:
        print(f"[memory] failed reading knowledge source titles: {e}", file=sys.stderr)

    cached_fn = getattr(_pkg(), "_cached_entries", _cached_entries)
    cache = cached_fn()
    kid_fn = getattr(_pkg(), "_knowledge_id_from_path", _knowledge_id_from_path)
    idxs, entries = [], []
    for i, (source, path, text, v) in enumerate(cache["entries"]):
        if source != "knowledge":
            continue
        kid = kid_fn(path)
        if kid is not None and kid in allowed_knowledge_source_ids:
            idxs.append(i)
            entries.append((source, path, text, v, kid))
    if not entries:
        return []

    embed_fn = getattr(_pkg(), "_embed_texts", _embed_texts)
    qv = await embed_fn([query])
    qvec = qv[0] if qv else None

    q_lower = query.lower()
    words = [w.lower() for w in re.findall(r"\w{3,}", query)][:10]

    q_word_set = set(re.findall(r"\w+", q_lower))
    matched_title_kids = set()
    for kid, title in source_titles.items():
        t_words = [tw.lower() for tw in re.findall(r"\w{3,}", title)
                   if tw.lower() not in {"the", "and", "for", "with"}]
        if any(tw in q_word_set for tw in t_words):
            matched_title_kids.add(kid)

    is_broad_kb = any(phrase in q_lower for phrase in (
        "company knowledge base", "knowledge base", "company info", "company data",
        "our company", "all data", "show data", "employee base", "emplyee base",
        "employee data", "what data", "internal data", "company documents"
    ))

    lex_raw, title_boost, query_lex = [], [], []
    cosines_fn = getattr(_pkg(), "_cosines", _cosines)
    cos_raw = cosines_fn(cache, idxs, qvec)
    lower = cache["lower"]
    for j, (_source, _path, text, v, kid) in enumerate(entries):
        tl = lower[idxs[j]]
        lex = float(sum(tl.count(w) for w in words))
        query_lex.append(lex)
        if kid in source_titles:
            s_title = source_titles[kid].lower()
            lex += float(sum(tl.count(tw) for tw in re.findall(r"\w{3,}", s_title)))
        lex_raw.append(lex)

        boost = 0.0
        if kid in matched_title_kids:
            boost += 0.35
        elif is_broad_kb:
            boost += 0.20
        title_boost.append(boost)

    norm_fn = getattr(_pkg(), "_norm", _norm)
    lex_n = norm_fn(lex_raw)
    cos_n = norm_fn(cos_raw) if qvec is not None else [0.0] * len(entries)

    results = []
    for i, (source, path, text, _v, kid) in enumerate(entries):
        if not title_boost[i]:
            relevant = (cos_raw[i] >= min_cos) if (qvec is not None and _v is not None) \
                else (query_lex[i] >= min_lex)
            if not relevant:
                continue
        base_score = (0.5 * cos_n[i] + 0.5 * lex_n[i]) if qvec is not None else lex_n[i]
        final_score = min(1.0, base_score + title_boost[i])
        title = source_titles.get(kid, f"Knowledge Source #{kid}")
        results.append({
            "source": source,
            "path": path,
            "source_id": kid,
            "title": title,
            "text": text,
            "score": final_score,
            "cos": cos_raw[i],
        })
    if not results:
        return []

    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:max(1, k)]
