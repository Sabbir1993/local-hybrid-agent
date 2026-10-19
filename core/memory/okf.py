"""
core/memory/okf.py - Object Knowledge Format (OKF) & Entity Link Graph Store.

Implements the structured facts and entity link graph layer for enterprise RAG:
1. Deterministic entity and relation extraction from documents at ingest.
2. Relational triple storage (Subject, Predicate, Object) in SQLite.
3. Graph link traversal and multi-hop fact retrieval.
4. Clean separation from dense vector and sparse BM25 indices.
"""

import json
import re
import sys
from typing import Optional, Set, List, Dict, Tuple

# Common organizational & entity relation patterns
_RELATION_PATTERNS = [
    # (Regex pattern, predicate_name, reverse_predicate)
    (re.compile(r"([A-Z][A-Za-z0-9_\-\s]{2,40}?)\s+(?:reports to|is supervised by|is managed by)\s+([A-Z][A-Za-z0-9_\-\s]{2,40})", re.I),
     "reports_to", "manages"),
    (re.compile(r"([A-Z][A-Za-z0-9_\-\s]{2,40}?)\s+(?:is part of|belongs to|is a member of)\s+([A-Z][A-Za-z0-9_\-\s]{2,40})", re.I),
     "belongs_to", "contains"),
    (re.compile(r"([A-Z][A-Za-z0-9_\-\s]{2,40}?)\s+(?:depends on|requires|relies on)\s+([A-Z][A-Za-z0-9_\-\s]{2,40})", re.I),
     "depends_on", "required_by"),
    (re.compile(r"([A-Z][A-Za-z0-9_\-\s]{2,40}?)\s+(?:is responsible for|leads|heads)\s+([A-Z][A-Za-z0-9_\-\s]{2,40})", re.I),
     "leads", "led_by"),
    (re.compile(r"([A-Z][A-Za-z0-9_\-\s]{2,40}?)\s+(?:is defined as|refers to|stands for)\s+([^.\n]{4,100})", re.I),
     "defined_as", "definition_of"),
    (re.compile(r"([A-Z][A-Za-z0-9_\-\s]{2,40}?)\s+(?:governed by|complies with|subject to)\s+([A-Z][A-Za-z0-9_\-\s]{2,40})", re.I),
     "governed_by", "governs"),
]

_DEFINITION_LINE_RX = re.compile(r"^([A-Z][A-Za-z0-9_\-\s]{1,35})\s*[:\-=]\s*([^\n]{5,150})$", re.M)
_HEADER_RX = re.compile(r"^(#{1,4})\s+(.+)$", re.M)


def _get_db():
    from .store import _db
    return _db()


def init_okf_tables(conn) -> None:
    """Initialize SQLite tables for entities, relations, and document chunk links."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS kb_entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            aliases TEXT,
            UNIQUE(source_id, name, entity_type)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_kb_entities_source ON kb_entities(source_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_kb_entities_name ON kb_entities(name COLLATE NOCASE)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS kb_relations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL,
            source_entity TEXT NOT NULL,
            predicate TEXT NOT NULL,
            target_entity TEXT NOT NULL,
            chunk_idx INTEGER,
            weight REAL DEFAULT 1.0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_kb_relations_source ON kb_relations(source_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_kb_relations_pair ON kb_relations(source_entity COLLATE NOCASE, target_entity COLLATE NOCASE)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS kb_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL,
            chunk_idx_a INTEGER NOT NULL,
            chunk_idx_b INTEGER NOT NULL,
            link_type TEXT NOT NULL,
            weight REAL DEFAULT 1.0,
            UNIQUE(source_id, chunk_idx_a, chunk_idx_b, link_type)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_kb_links_source ON kb_links(source_id)")


def extract_okf_graph(source_id: int, text: str, chunks: Optional[List[str]] = None) -> Tuple[List[dict], List[dict], List[dict]]:
    """Extract entities, relations, and intra-document links from text content."""
    entities: List[dict] = []
    relations: List[dict] = []
    links: List[dict] = []

    seen_entities = set()
    seen_relations = set()

    def add_entity(name: str, etype: str):
        cleaned = name.strip()
        if len(cleaned) < 2 or len(cleaned) > 80:
            return
        key = (cleaned.lower(), etype)
        if key not in seen_entities:
            seen_entities.add(key)
            entities.append({
                "source_id": source_id,
                "name": cleaned,
                "entity_type": etype,
                "aliases": json.dumps([cleaned])
            })

    def add_relation(src: str, pred: str, tgt: str, chunk_idx: Optional[int] = None, weight: float = 1.0):
        src_clean = src.strip()
        tgt_clean = tgt.strip()
        if not src_clean or not tgt_clean or src_clean.lower() == tgt_clean.lower():
            return
        key = (src_clean.lower(), pred.lower(), tgt_clean.lower())
        if key not in seen_relations:
            seen_relations.add(key)
            relations.append({
                "source_id": source_id,
                "source_entity": src_clean,
                "predicate": pred,
                "target_entity": tgt_clean,
                "chunk_idx": chunk_idx,
                "weight": weight
            })
            add_entity(src_clean, "concept")
            add_entity(tgt_clean, "concept")

    # 1. Structural extraction from Markdown headers
    current_h1 = None
    current_h2 = None
    for line in text.splitlines():
        h_match = _HEADER_RX.match(line.strip())
        if h_match:
            level = len(h_match.group(1))
            htext = h_match.group(2).strip()
            add_entity(htext, "section_header")
            if level == 1:
                current_h1 = htext
                current_h2 = None
            elif level == 2:
                current_h2 = htext
                if current_h1:
                    add_relation(current_h2, "subsection_of", current_h1)
            elif level >= 3:
                parent = current_h2 or current_h1
                if parent:
                    add_relation(htext, "subsection_of", parent)

    # 2. Key-value and definition lines
    for line in text.splitlines():
        line = line.strip()
        d_match = _DEFINITION_LINE_RX.match(line)
        if d_match:
            term = d_match.group(1).strip()
            desc = d_match.group(2).strip()
            add_relation(term, "defined_as", desc)

    # 3. Sentence relation patterns per chunk
    if chunks:
        for idx, chunk in enumerate(chunks):
            # Check sequential links with previous chunk
            if idx > 0:
                links.append({
                    "source_id": source_id,
                    "chunk_idx_a": idx - 1,
                    "chunk_idx_b": idx,
                    "link_type": "sequential",
                    "weight": 0.8
                })

            for pattern, pred, _rev in _RELATION_PATTERNS:
                for match in pattern.finditer(chunk):
                    s = match.group(1).strip()
                    t = match.group(2).strip()
                    add_relation(s, pred, t, chunk_idx=idx, weight=1.0)

    return entities, relations, links


def index_okf_source(source_id: int, text: str, chunks: Optional[List[str]] = None) -> Tuple[int, int, int]:
    """Extract and persist OKF entities, relations, and links for a knowledge source."""
    entities, relations, links = extract_okf_graph(source_id, text, chunks)
    db = _get_db()
    try:
        delete_okf_source(source_id)
        if entities:
            db.executemany(
                "INSERT OR IGNORE INTO kb_entities (source_id, name, entity_type, aliases) VALUES (?, ?, ?, ?)",
                [(e["source_id"], e["name"], e["entity_type"], e["aliases"]) for e in entities]
            )
        if relations:
            db.executemany(
                "INSERT INTO kb_relations (source_id, source_entity, predicate, target_entity, chunk_idx, weight) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(r["source_id"], r["source_entity"], r["predicate"], r["target_entity"], r["chunk_idx"], r["weight"])
                 for r in relations]
            )
        if links:
            db.executemany(
                "INSERT OR IGNORE INTO kb_links (source_id, chunk_idx_a, chunk_idx_b, link_type, weight) "
                "VALUES (?, ?, ?, ?, ?)",
                [(l["source_id"], l["chunk_idx_a"], l["chunk_idx_b"], l["link_type"], l["weight"]) for l in links]
            )
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"[okf] error indexing source {source_id}: {e}", file=sys.stderr)
    return len(entities), len(relations), len(links)


def delete_okf_source(source_id: int) -> None:
    """Delete all entities, relations, and links belonging to a source."""
    db = _get_db()
    try:
        db.execute("DELETE FROM kb_entities WHERE source_id=?", (source_id,))
        db.execute("DELETE FROM kb_relations WHERE source_id=?", (source_id,))
        db.execute("DELETE FROM kb_links WHERE source_id=?", (source_id,))
        db.commit()
    except Exception as e:
        print(f"[okf] error deleting okf data for source {source_id}: {e}", file=sys.stderr)


def search_okf(query: str, allowed_source_ids: Optional[Set[int]] = None, k: int = 5) -> List[Dict]:
    """Retrieve structured facts matching entities or relations in the query."""
    if not query or not query.strip():
        return []

    db = _get_db()
    tokens = [t for t in re.findall(r"\w{3,}", query.lower()) if len(t) >= 3]
    if not tokens:
        return []

    # Build SQL filter for allowed sources
    source_filter = ""
    params: List = []
    if allowed_source_ids:
        placeholders = ",".join("?" for _ in allowed_source_ids)
        source_filter = f" AND source_id IN ({placeholders})"
        params.extend(list(allowed_source_ids))

    hits = []
    # Query direct matching relations (either source or target entity matches tokens)
    sql_clauses = " OR ".join(["source_entity LIKE ? OR target_entity LIKE ?" for _ in tokens[:6]])
    full_sql = f"SELECT source_id, source_entity, predicate, target_entity, chunk_idx, weight FROM kb_relations WHERE ({sql_clauses}){source_filter} LIMIT {k * 3}"

    q_params = []
    for t in tokens[:6]:
        like_p = f"%{t}%"
        q_params.extend([like_p, like_p])
    q_params.extend(params)

    try:
        rows = db.execute(full_sql, q_params).fetchall()
        for r in rows:
            sid, src_ent, pred, tgt_ent, chunk_idx, weight = r
            fact_str = f"{src_ent} [{pred}] {tgt_ent}"
            # Score bonus if both entities match or exact phrase is in query
            q_lower = query.lower()
            bonus = 0.0
            if src_ent.lower() in q_lower:
                bonus += 0.3
            if tgt_ent.lower() in q_lower:
                bonus += 0.3
            score = min(1.0, 0.6 * weight + bonus)
            hits.append({
                "source_id": sid,
                "fact": fact_str,
                "source_entity": src_ent,
                "predicate": pred,
                "target_entity": tgt_ent,
                "chunk_idx": chunk_idx,
                "score": score,
                "modality": "okf"
            })
    except Exception as e:
        print(f"[okf] search_okf failed: {e}", file=sys.stderr)

    hits.sort(key=lambda x: x["score"], reverse=True)
    return hits[:k]


def backfill_okf_from_chunks() -> int:
    """Extract and index OKF entities & relations for any existing knowledge chunks missing OKF data."""
    db = _get_db()
    try:
        rows = db.execute("""
            SELECT DISTINCT path FROM chunks
            WHERE source='knowledge'
            AND path LIKE 'kb:%'
        """).fetchall()
        indexed_sources = {r[0] for r in db.execute("SELECT DISTINCT source_id FROM kb_relations").fetchall()}

        backfilled = 0
        for (path,) in rows:
            try:
                sid = int(path.split(":")[1])
            except (ValueError, IndexError):
                continue
            if sid in indexed_sources:
                continue
            chunk_rows = db.execute(
                "SELECT chunk_idx, text FROM chunks WHERE source='knowledge' AND path=? ORDER BY chunk_idx",
                (path,)
            ).fetchall()
            if not chunk_rows:
                continue
            chunk_texts = [r[1] for r in chunk_rows]
            full_text = "\n\n".join(chunk_texts)
            index_okf_source(sid, full_text, chunk_texts)
            backfilled += 1
        return backfilled
    except Exception as e:
        print(f"[okf] backfill failed: {e}", file=sys.stderr)
        return 0
