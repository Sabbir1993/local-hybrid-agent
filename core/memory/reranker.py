"""
core/memory/reranker.py - Hybrid Candidate Fusion, Re-ranking & Evidence Filtering.

Implements the second-stage ranking layer:
1. Deduplication across heterogeneous candidate lists (OKF, BM25, Vector DB).
2. Cross-feature re-ranking blending lexical coverage, symbol precision, semantic cosine, and entity overlap.
3. Strict evidence filtering: drops low-confidence noise chunks to prevent hallucinations.
"""

import re
from typing import List, Dict, Optional


def _tokenize(text: str) -> List[str]:
    """Tokenize into alphanumeric words with symbols preserved."""
    return [w.lower() for w in re.findall(r"[A-Za-z0-9_\-\.]{2,}", text)]


def deduplicate_candidates(candidates: List[Dict]) -> List[Dict]:
    """Deduplicate candidate chunks based on (path/source_id, chunk_idx) and text fingerprint."""
    seen_keys = set()
    seen_texts = set()
    deduped = []

    for c in candidates:
        # Key based on path / chunk_idx or source_id
        path = c.get("path") or f"kb:{c.get('source_id')}"
        chunk_idx = c.get("chunk_idx")
        key = (path, chunk_idx) if chunk_idx is not None else None

        text = (c.get("text") or c.get("fact") or "").strip()
        text_fp = re.sub(r"\s+", " ", text[:120].lower())

        if key and key in seen_keys:
            continue
        if text_fp and text_fp in seen_texts:
            continue

        if key:
            seen_keys.add(key)
        if text_fp:
            seen_texts.add(text_fp)

        deduped.append(c)

    return deduped


def calculate_cross_features(query: str, text: str, base_score: float = 0.5) -> float:
    """Calculate multi-signal relevance score between query and candidate text.

    Signals:
    1. Exact query phrase match.
    2. Token overlap ratio (Jaccard / coverage).
    3. Exact symbol / identifier preservation.
    """
    if not query or not text:
        return 0.0

    q_lower = query.lower()
    t_lower = text.lower()

    # Exact phrase presence
    phrase_bonus = 0.25 if len(q_lower) > 5 and q_lower in t_lower else 0.0

    # Token overlap
    q_tokens = _tokenize(query)
    if not q_tokens:
        return base_score

    t_tokens_set = set(_tokenize(text))
    matched_tokens = sum(1 for tok in q_tokens if tok in t_tokens_set)
    coverage = matched_tokens / max(1, len(q_tokens))

    # Exact symbol bonus (CamelCase, snake_case, or numbers)
    symbols = [w for w in re.findall(r"\b[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)+\b|[A-Z][a-z]+[A-Z]\w*|\b[A-Z]{2,}\b|\b\d{3,}\b", query)]
    symbol_bonus = 0.0
    if symbols:
        matched_symbols = sum(1 for sym in symbols if sym in text)
        symbol_bonus = 0.20 * (matched_symbols / len(symbols))

    final_score = (0.45 * base_score) + (0.35 * coverage) + phrase_bonus + symbol_bonus
    return min(1.0, max(0.0, final_score))


def rerank_and_filter(
    query: str,
    candidates: List[Dict],
    top_k: int = 6,
    min_evidence_score: float = 0.30,
    okf_facts: Optional[List[Dict]] = None
) -> List[Dict]:
    """Deduplicate, re-rank across multi-modal evidence, and filter out low-confidence noise."""
    if not candidates and not okf_facts:
        return []

    # 1. Deduplicate text candidates
    unique_candidates = deduplicate_candidates(candidates)

    # 2. Re-score each candidate chunk
    scored_candidates = []
    entity_tokens = set()
    if okf_facts:
        for f in okf_facts:
            entity_tokens.update(_tokenize(f.get("source_entity", "")))
            entity_tokens.update(_tokenize(f.get("target_entity", "")))

    for c in unique_candidates:
        text = c.get("text", "")
        raw_score = float(c.get("score", 0.5))
        rerank_score = calculate_cross_features(query, text, base_score=raw_score)

        # OKF entity confirmation boost
        if entity_tokens and any(tok in text.lower() for tok in entity_tokens):
            rerank_score = min(1.0, rerank_score + 0.15)

        # Evidence filtering: ignore candidates below confidence cutoff
        if rerank_score >= min_evidence_score:
            c_copy = dict(c)
            c_copy["final_score"] = round(rerank_score, 4)
            c_copy["score"] = round(rerank_score, 4)
            scored_candidates.append(c_copy)

    # 3. Sort by final rerank score descending
    scored_candidates.sort(key=lambda x: x["final_score"], reverse=True)
    return scored_candidates[:top_k]
