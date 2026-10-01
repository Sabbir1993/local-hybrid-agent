import re
import sys
from typing import Optional

from .cache import _cached_entries, _cosines
from .constants import (_knowledge_id_from_path, _norm, _normalize_query_words,
                        _session_id_from_path)
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
                                  # R4 grid (144 combos x fake+real, knee cited):
                                  # min_cos 0.55 halves real fallout (0.771 ->
                                  # 0.407) for one hard paraphrase miss; 0.65
                                  # collapses paraphrase to 0.6 (vetoed).
                                  # min_lex 2 is the lexonly knee (1.0/0.369).
                                  # Weights stay 0.5/0.5 for lack of evidence:
                                  # 0/48 grid cells varied with them in EITHER
                                  # mode (this corpus cannot price them - needs
                                  # adversarial lexical-vs-semantic questions).
                                  min_cos: float = 0.55, min_lex: int = 2,
                                  cos_weight: float = 0.5, lex_weight: float = 0.5,
                                  title_boost_match: float = 0.35,
                                  title_boost_broad: float = 0.20,
                                  # R3 measured title-lex counts at exactly zero
                                  # contribution (slice A vs B identical in every
                                  # mode): raw title-word counts double-count the
                                  # match boost. Off by default; knob retained
                                  # for the R4 grid in case recalibrated weights
                                  # interact with it.
                                  use_title_lex: bool = False,
                                  # R6 second-stage ordering (web rerank.py
                                  # pattern): reorder a broad candidate set by
                                  # rank fusion over cosine/lexical/title
                                  # orderings instead of rescoring with the
                                  # same weights. R6 slice: ON with stage-1 0.45
                                  # recovers the R4 paraphrase casualty at rank
                                  # 2 (R 0.944->1.0, F 0.412->0.44 - accepted
                                  # 2:1 knee). Stage-1 0.35 + floor measures
                                  # identical-to-off (dead path); floor off
                                  # floods (F 0.831 - vetoed).
                                  rerank: bool = True,
                                  rerank_candidates: int = 20,
                                  rerank_min_cos: float = 0.45,
                                  # Order without a floor is a shuffler: the
                                  # fused order still has to earn its place
                                  # against the stage-0 relevance gate.
                                  rerank_floor: bool = True) -> list:
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
    # Normalized once: stopword-filtered + stemmed, so "receipts" counts doc
    # "receipt" and "are/how/do" stop inflating every score. Both sides (query
    # words, title words) normalize identically - matching is substring
    # counting, so any skew blinds whole question classes.
    words = _normalize_query_words(query)

    q_word_set = set(_normalize_query_words(query, limit=100))
    matched_title_kids = set()
    title_lex_words = {}
    for kid, title in source_titles.items():
        t_words = _normalize_query_words(title, limit=50)
        title_lex_words[kid] = t_words
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
        if use_title_lex and kid in source_titles:
            lex += float(sum(tl.count(tw) for tw in title_lex_words.get(kid, ())))
        lex_raw.append(lex)

        boost = 0.0
        if kid in matched_title_kids:
            boost += title_boost_match
        elif is_broad_kb:
            boost += title_boost_broad
        title_boost.append(boost)

    norm_fn = getattr(_pkg(), "_norm", _norm)
    lex_n = norm_fn(lex_raw)
    cos_n = norm_fn(cos_raw) if qvec is not None else [0.0] * len(entries)

    results = []
    for i, (source, path, text, _v, kid) in enumerate(entries):
        # Stage-1 gate: broad when reranking (the second stage reorders, so
        # the first stage must not pre-filter what it cannot judge), tight
        # otherwise. Title-boosted entries skip the gate either way.
        stage_cos = rerank_min_cos if rerank else min_cos
        if not title_boost[i]:
            relevant = (cos_raw[i] >= stage_cos) if (qvec is not None and _v is not None) \
                else (query_lex[i] >= min_lex)
            if not relevant:
                continue
        base_score = (cos_weight * cos_n[i] + lex_weight * lex_n[i]) if qvec is not None else lex_n[i]
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
            "_lex": lex_raw[i],
            "_qlex": query_lex[i],
            "_vok": _v is not None,
            "_boost": title_boost[i],
        })
    if not results:
        return []
    results.sort(key=lambda r: r["score"], reverse=True)
    if rerank and len(results) > 2:
        cands = results[:max(rerank_candidates, k)]
        fused = _rrf_reorder(cands, has_cos=qvec is not None)
        if rerank_floor:
            # Same gate as stage 0: the fused order decides RANKING, the gate
            # decides MEMBERSHIP. Without this the loose stage-1 gate floods
            # the top-k with reordered noise (measured: fallout 0.412->0.831).
            # Mirrors the stage-0 vec-None fallback exactly (query_lex when a
            # chunk has no stored vector).
            def _passes(r):
                if r["_boost"]:
                    return True
                if qvec is not None and r["_vok"]:
                    return r["cos"] >= min_cos
                return r["_qlex"] >= min_lex
            results = [r for r in fused if _passes(r)]
        else:
            results = fused
    else:
        results = results[:max(1, k)]
    for r in results[:max(1, k)]:
        r.pop("_lex", None)
        r.pop("_qlex", None)
        r.pop("_vok", None)
        r.pop("_boost", None)
    return results[:max(1, k)]


def _rrf_reorder(cands: list, has_cos: bool = True, k_rrf: int = 60) -> list:
    """Reciprocal-rank fusion over independent orderings (web rerank.py pattern).

    Each ordering (cosine desc, lexical desc, title-boost desc) votes
    1/(k_rrf + rank); the fused order is the second opinion on a candidate
    set the weighted sum already produced. Pure-python over <= 20 items:
    microseconds, no fresh embeddings (query + stored chunk vecs reused).
    Without a query vector the cosine ordering drops out, mirroring the web
    fallback (engines + lexical only).
    """
    orderings = [[id(c) for c in sorted(cands, key=lambda c: -c["_lex"])],
                 [id(c) for c in sorted(cands, key=lambda c: -c["_boost"])]]
    if has_cos:
        orderings.append([id(c) for c in sorted(cands, key=lambda c: -c["cos"])])
    rrf = {}
    for ordering in orderings:
        for rank, key in enumerate(ordering):
            rrf[key] = rrf.get(key, 0.0) + 1.0 / (k_rrf + rank)
    return sorted(cands, key=lambda c: -rrf[id(c)])
