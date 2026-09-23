"""
core/knowledge_router.py - Company Knowledge Base Router & Context Generator.

Routes incoming prompts requesting company, internal, HR, or organizational
information to the authentic organizational knowledge base, preventing model
hallucination, dummy file fabrication, and ensuring retrieval passes through
the output sanitizer before reaching the user.
"""

import re
import sys
from typing import Optional, Set, List, Dict, Tuple
from .auth_db import list_knowledge_sources
from .memory import search_knowledge_hybrid, _db, _embed_texts, _store_vecs

COMPANY_KEYWORDS = {
    "company", "companies", "knowledge base", "knowledgebase", "kb",
    "organization", "organisational", "organizational", "corporate",
    "internal", "our company", "company's", "workplace", "firm",
    "employee", "employees", "emplyee", "emplyees", "emp", "staff",
    "personnel", "workforce", "hr", "human resources", "payroll",
    "salary", "salaries", "compensation", "benefit", "benefits",
    "designation", "designations", "manager", "management", "lead",
    "policy", "policies", "handbook", "leave", "holiday", "attendance",
    "headquarters", "office", "founder", "ceo", "director", "ssl wireless",
    "sslwireless", "ssl", "ushop", "miot", "health facility",
    "engineering", "software analyst", "app analyst", "solution architect",
    "database administrator", "qa", "developer", "accounts", "finance"
}


def _get_active_source_titles(allowed_source_ids: Optional[Set[int]] = None) -> Dict[int, str]:
    """Retrieve mapping of source_id -> title for ready sources."""
    out = {}
    try:
        sources = list_knowledge_sources()
        for s in sources:
            s_dict = dict(s)
            sid = s_dict["id"]
            if allowed_source_ids is not None and sid not in allowed_source_ids:
                continue
            if s_dict.get("status") == "ready":
                out[sid] = s_dict.get("title") or f"Source #{sid}"
    except Exception as e:
        print(f"[knowledge_router] error listing sources: {e}", file=sys.stderr)
    return out


def is_company_or_kb_query(query: str, allowed_source_ids: Optional[Set[int]] = None) -> bool:
    """Check if query is directed at company info, internal docs, or the knowledge base."""
    if not query or not query.strip():
        return False
    q_lower = query.lower()

    # Check for direct company keywords/phrases
    for kw in COMPANY_KEYWORDS:
        if kw in q_lower:
            return True

    # Check against titles of active knowledge sources
    titles = _get_active_source_titles(allowed_source_ids)
    for title in titles.values():
        t_lower = title.lower()
        if t_lower in q_lower:
            return True
        # Check individual substantive words in title (e.g. "Platinum", "Club", "Facility")
        t_words = [w for w in re.findall(r"\w{3,}", t_lower) if w not in {"the", "and", "for", "with"}]
        if t_words and any(tw in q_lower for tw in t_words):
            return True

    return False


async def ensure_knowledge_vectors():
    """Ensure any newly uploaded or unvectorized knowledge chunks are embedded."""
    try:
        rows = _db().execute(
            "SELECT source, path, chunk_idx, text, version FROM chunks WHERE source='knowledge' AND vec IS NULL"
        ).fetchall()
        if rows:
            vecs = await _embed_texts([r[3] for r in rows])
            if vecs:
                _store_vecs(rows, vecs)
    except Exception as e:
        print(f"[knowledge_router] vector backfill failed: {e}", file=sys.stderr)


async def fetch_company_knowledge(query: str, allowed_source_ids: Set[int], k: int = 6) -> Tuple[List[Dict], str]:
    """Fetch relevant knowledge chunks and generate the authoritative reference prompt block.

    Returns (hits_list, system_prompt_block).
    """
    if not allowed_source_ids:
        return [], ""

    await ensure_knowledge_vectors()

    hits = await search_knowledge_hybrid(query, k=k, allowed_knowledge_source_ids=allowed_source_ids)
    if not hits:
        return [], ""

    # Group chunks by source title
    by_source: Dict[str, List[str]] = {}
    for h in hits:
        title = h.get("title") or f"Knowledge Source #{h.get('source_id')}"
        by_source.setdefault(title, []).append(h["text"])

    sections = []
    for title, texts in by_source.items():
        doc_content = "\n\n".join(texts)
        sections.append(f"### Source: {title}\n{doc_content}")

    knowledge_text = "\n\n---\n\n".join(sections)

    prompt_block = (
        "ORGANIZATIONAL KNOWLEDGE BASE (AUTHENTIC INTERNAL COMPANY DATA):\n"
        "The following material is retrieved directly from our company's internal knowledge base:\n\n"
        f"{knowledge_text}\n\n"
        "CRITICAL OPERATING RULES FOR COMPANY DATA:\n"
        "1. You MUST answer the user's inquiry strictly and accurately using the authentic internal knowledge base data above.\n"
        "2. NEVER hallucinate, invent, fabricate, or generate dummy employee names, fake records, or placeholder datasets.\n"
        "3. NEVER call `write_file` to create sample or dummy CSV/Excel spreadsheets with fictional data when asked for company data. "
        "Provide the real data directly in your text answer.\n"
        "4. If and only if the user explicitly asks to export or save the data into a file (e.g. 'export this to CSV', 'make an excel sheet of these employees'), "
        "you may call `write_file` using exclusively the authentic records from the knowledge base above.\n"
        "5. If specific company information is not in the knowledge base, state clearly: "
        "'The requested information was not found in the company knowledge base.' Never fabricate an answer."
    )

    return hits, prompt_block
