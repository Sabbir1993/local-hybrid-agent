"""
core/memory.py - Real semantic memory for Local Agent.

Wires the nomic-embed embedder (configured on port 8093, previously never
called) into a persistent sqlite vector store, and backs the `search_memory`
tool with hybrid recall: cosine similarity (semantic) + keyword overlap
(lexical), both min-max normalized and averaged.

  * workspace files: text-ish extensions, <=512KB, chunked ~900 chars / 150 overlap
  * past sessions: title + first user message (from projects.db via core.db)
  * incremental mtime/version-based indexing; runs as a background task at boot
  * pure-python cosine fallback when numpy is unavailable
  * graceful degradation: embedder down -> lexical-only recall, no crashes

Indexing never blocks request handling.
"""

import asyncio
import json
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from .config import MEMORY_DB_FILE
from .sqlite_util import ThreadLocalDB

try:
    import numpy as _np
except Exception:
    _np = None

CHUNK_CHARS = 900
CHUNK_OVERLAP = 150
MAX_FILE_BYTES = 512 * 1024
MAX_FILES = 400
MAX_CHUNKS_PER_DOC = 40
MAX_SESSIONS = 60
EMBED_BATCH = 16
EMBEDDER_RETRY_S = 600  # retry the embedder 10 min after a failure
TEXT_EXTS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".css", ".json", ".md",
    ".txt", ".csv", ".yml", ".yaml", ".xml", ".sh", ".bat", ".ps1", ".php",
    ".sql", ".ini", ".toml", ".log",
}
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv"}

_conn: Optional[sqlite3.Connection] = None
_index_lock = asyncio.Lock()
_last_index = 0.0
_embedder_down_until = 0.0


def _db() -> ThreadLocalDB:
    global _conn
    if _conn is None:
        _conn = ThreadLocalDB(MEMORY_DB_FILE)
        _conn.execute("""
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                path TEXT NOT NULL,
                chunk_idx INTEGER NOT NULL,
                text TEXT NOT NULL,
                vec BLOB,
                dim INTEGER,
                version REAL,
                UNIQUE(source, path, chunk_idx)
            )
        """)
        _conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        # chunks_gen changes whenever any connection (db explorer included)
        # writes chunks, so the in-process search cache knows when to reload.
        _conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('chunks_gen', '0')")
        for ev in ("INSERT", "UPDATE", "DELETE"):
            _conn.execute(f"""CREATE TRIGGER IF NOT EXISTS chunks_gen_{ev.lower()} AFTER {ev} ON chunks
                BEGIN UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'chunks_gen'; END""")
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_novec ON chunks(source) WHERE vec IS NULL")
        _conn.commit()
    return _conn


def _chunk_text(text: str) -> list:
    text = text.strip()
    if not text:
        return []
    if len(text) <= CHUNK_CHARS:
        return [text]
    chunks = []
    step = CHUNK_CHARS - CHUNK_OVERLAP
    i = 0
    while i < len(text) and len(chunks) < MAX_CHUNKS_PER_DOC:
        part = text[i:i + CHUNK_CHARS]
        if part.strip():
            chunks.append(part)
        i += step
    return chunks


async def _embed_texts(texts: list) -> Optional[list]:
    """Embed via the already-running nomic-embed llama-server (port 8093)."""
    global _embedder_down_until
    if not texts:
        return None
    if time.time() < _embedder_down_until:
        return None
    try:
        from .small_model import small_models
        inst = small_models.instances["embedder"]
        if not inst.available:
            _embedder_down_until = time.time() + EMBEDDER_RETRY_S
            return None
        await inst.ensure_loaded()
        out = []
        for i in range(0, len(texts), EMBED_BATCH):
            batch = [t[:4000] for t in texts[i:i + EMBED_BATCH]]
            r = await inst.client.post("/v1/embeddings", json={"input": batch})
            r.raise_for_status()
            data = r.json().get("data") or []
            out.extend(item.get("embedding") or [] for item in data)
            inst.last_used = time.time()
        if len(out) != len(texts) or any(not v for v in out):
            return None
        return out
    except Exception as e:
        print(f"[memory] embedder unavailable ({e}) - lexical-only recall", file=sys.stderr)
        _embedder_down_until = time.time() + EMBEDDER_RETRY_S
        return None


def _store_vecs(rows: list, vecs: Optional[list]) -> None:
    """rows: (source, path, chunk_idx, text, version); vecs parallel or None."""
    recs = []
    for i, (source, path, idx, text, version) in enumerate(rows):
        if vecs is not None:
            v = vecs[i]
            if _np is not None:
                blob = sqlite3.Binary(_np.asarray(v, dtype=_np.float32).tobytes())
            else:
                blob = sqlite3.Binary(json.dumps(v).encode())
            recs.append((source, path, idx, text, blob, len(v), version))
        else:
            # indexed lexically now; vec stays NULL so a later embedder pass
            # re-embeds this chunk (version match requires vec IS NOT NULL)
            recs.append((source, path, idx, text, None, None, version))
    _db().executemany(
        "INSERT INTO chunks (source, path, chunk_idx, text, vec, dim, version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(source, path, chunk_idx) DO UPDATE SET "
        "text=excluded.text, vec=excluded.vec, dim=excluded.dim, version=excluded.version",
        recs,
    )
    _db().commit()


def _already_indexed(source: str, path: str, version: float) -> bool:
    row = _db().execute(
        "SELECT version FROM chunks WHERE source=? AND path=? AND vec IS NOT NULL LIMIT 1",
        (source, path),
    ).fetchone()
    return bool(row) and abs((row[0] or 0) - version) < 1e-6


def _delete_stale(source: str, seen: set) -> None:
    stale = [r[0] for r in _db().execute(
        "SELECT DISTINCT path FROM chunks WHERE source=?", (source,)).fetchall()
        if r[0] not in seen]
    if stale:
        _db().execute(
            f"DELETE FROM chunks WHERE source=? AND path IN ({','.join('?' * len(stale))})",
            [source] + stale,
        )
        _db().commit()


async def index_workspace(ws_path: Optional[Path] = None, force: bool = False) -> dict:
    """Incrementally index workspace files into the vector store.

    Only an explicitly passed path is indexed. The implicit default used to be
    the active workspace, or WORKSPACE_ROOT when there was none -- the
    background task has no user, so it walked the server's shared workspace
    (all users' folders). Project files live on users' machines (companion),
    so there is nothing server-side to index by default.
    """
    if ws_path is None:
        return {"files": 0, "chunks": 0}
    ws = Path(ws_path)
    if not ws.exists():
        return {"files": 0, "chunks": 0}
    files = []
    try:
        for f in ws.rglob("*"):
            if len(files) >= MAX_FILES:
                break
            if not f.is_file():
                continue
            try:
                rel = str(f.relative_to(ws)).replace("\\", "/")
            except ValueError:
                continue
            # skip-check on RELATIVE parts only (absolute parts always contain
            # the workspace root folder name itself)
            if any(part in SKIP_DIRS for part in Path(rel).parts):
                continue
            if f.suffix.lower() not in TEXT_EXTS:
                continue
            try:
                if f.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            files.append(f)
    except Exception as e:
        print(f"[memory] workspace walk failed: {e}", file=sys.stderr)
        return {"files": 0, "chunks": 0}

    new_rows = []
    seen = set()
    for f in files:
        try:
            rel = str(f.relative_to(ws)).replace("\\", "/")
        except ValueError:
            continue
        seen.add(rel)
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        if not force and _already_indexed("workspace", rel, mtime):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for idx, part in enumerate(_chunk_text(text)):
            new_rows.append(("workspace", rel, idx, part, mtime))

    _delete_stale("workspace", seen)
    if new_rows:
        vecs = await _embed_texts([r[3] for r in new_rows])
        _store_vecs(new_rows, vecs)
    return {"files": len(files), "chunks": len(new_rows)}


async def index_sessions(force: bool = False) -> int:
    """Incrementally index past-session docs (title + first user message)."""
    try:
        from .db import db_session_docs
        docs = db_session_docs(MAX_SESSIONS)
    except Exception as e:
        print(f"[memory] session docs unavailable: {e}", file=sys.stderr)
        return 0
    rows = []
    seen = set()
    for path, text, version in docs:
        seen.add(path)
        if not force and _already_indexed("session", path, version):
            continue
        for idx, part in enumerate(_chunk_text(text)[:4]):
            rows.append(("session", path, idx, part, version))
    _delete_stale("session", seen)
    if rows:
        vecs = await _embed_texts([r[3] for r in rows])
        _store_vecs(rows, vecs)
    return len(rows)


async def index_knowledge_source(source_id: int, text: str, version: Optional[float] = None) -> int:
    """(Re)index one knowledge-base source's extracted text. Replaces any
    existing chunks for it. Returns the number of chunks stored."""
    path = f"kb:{source_id}"
    version = version if version is not None else time.time()
    _db().execute("DELETE FROM chunks WHERE source='knowledge' AND path=?", (path,))
    _db().commit()
    rows = [("knowledge", path, idx, part, version) for idx, part in enumerate(_chunk_text(text))]
    if rows:
        vecs = await _embed_texts([r[3] for r in rows])
        _store_vecs(rows, vecs)
    return len(rows)


def delete_knowledge_chunks(source_id: int) -> None:
    _db().execute("DELETE FROM chunks WHERE source='knowledge' AND path=?", (f"kb:{source_id}",))
    _db().commit()


def delete_session_chunks(session_ids) -> None:
    paths = [f"session:{int(i)}" for i in session_ids]
    if not paths:
        return
    _db().execute(f"DELETE FROM chunks WHERE source='session' AND path IN ({','.join('?' * len(paths))})",
                  paths)
    _db().commit()


def clear_chat_history_chunks() -> None:
    """Danger-zone: drop indexed chat/workspace memory (not the org knowledge
    base -- that's a separate, deliberately-curated source)."""
    _db().execute("DELETE FROM chunks WHERE source IN ('workspace', 'session')")
    _db().commit()


def _knowledge_id_from_path(path: str) -> Optional[int]:
    if path.startswith("kb:"):
        try:
            return int(path.split(":", 1)[1])
        except ValueError:
            return None
    return None


def _decode_vec(vec):
    if vec is None:
        return None
    try:
        raw = bytes(vec)
        if _np is not None:
            return _np.frombuffer(raw, dtype=_np.float32)
        return json.loads(raw.decode())
    except Exception:
        return None


# Search cache: every chunk with its lower-cased text, plus the vectors as one
# unit-normalised matrix, so a query costs one matrix-vector product instead
# of a SELECT of the whole table and a per-row Python loop. Rebuilt only when
# meta.chunks_gen moves (see the triggers in _db()).
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
    return list(_cached_entries()["entries"])


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


def _norm(vals: list) -> list:
    if not vals:
        return []
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        return [0.0] * len(vals)
    return [(v - lo) / (hi - lo) for v in vals]


def _session_id_from_path(path: str) -> Optional[int]:
    if path.startswith("session:"):
        try:
            return int(path.split(":", 1)[1])
        except ValueError:
            return None
    return None


async def search_memory_hybrid(query: str, k: int = 8, requesting_user_id: Optional[int] = None,
                                allowed_knowledge_source_ids: Optional[set] = None,
                                sources: Optional[set] = None) -> list:
    """Hybrid recall: 0.5*cosine + 0.5*lexical (lexical-only without embedder).

    Strict per-user isolation: "session"-sourced chunks (past chat titles/first
    messages) are filtered to the requesting user's own sessions only -- other
    users' chat history must never surface via memory search, even indirectly.
    "workspace" chunks are never returned: they were indexed from the server's
    shared WORKSPACE_ROOT (every user's folders, no owner recorded), so any
    stored rows are unattributable and must not leak across users.

    "knowledge"-sourced chunks (org knowledge base) are filtered to
    allowed_knowledge_source_ids *before* scoring -- an unpermitted caller's
    query never sees a restricted document as a retrieval candidate at all,
    which is what makes silence (no hit) safe instead of a leaky special case.
    When allowed_knowledge_source_ids is None, all knowledge chunks are
    excluded (callers must pass an explicit set, even if empty, to opt in).

    If `sources` is given (e.g. {'knowledge'}), entries are pre-filtered to
    only those sources before scoring and top-k truncation.
    """
    cache = _cached_entries()
    all_entries = cache["entries"]

    # Fail closed: a session chunk is returned only when its session still exists and
    # belongs to the caller -- orphaned chunks (deleted session, legacy NULL owner) and
    # callers with no user id get none.
    from .db import db_session_owner
    owners: dict = {}
    idxs = []
    for i, (source, path, _text, _v) in enumerate(all_entries):
        if source == "workspace" or (sources is not None and source not in sources):
            continue
        if source == "session":
            if requesting_user_id is None:
                continue
            sid = _session_id_from_path(path)
            if sid not in owners:
                owners[sid] = db_session_owner(sid) if sid is not None else None
            if owners[sid] != requesting_user_id:
                continue
        elif source == "knowledge":
            if not allowed_knowledge_source_ids:
                continue
            kid = _knowledge_id_from_path(path)
            if kid is None or kid not in allowed_knowledge_source_ids:
                continue
        idxs.append(i)
    if not idxs:
        return []
    qv = await _embed_texts([query])
    qvec = qv[0] if qv else None

    words = [w.lower() for w in re.findall(r"\w{3,}", query)][:8]
    lower = cache["lower"]
    lex_raw = [float(sum(lower[i].count(w) for w in words)) for i in idxs]
    cos_raw = _cosines(cache, idxs, qvec)

    lex_n = _norm(lex_raw)
    cos_n = _norm(cos_raw) if qvec is not None else None
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
    """Dedicated knowledge base retrieval scoped exclusively to allowed knowledge sources.

    Includes title-aware boosting from auth_db so broad company inquiries (e.g.
    'I want data from company knowledge base' or 'show employee base') reliably surface
    the relevant source chunks rather than starving out.

    Min-max normalisation always ranks *some* chunk at ~1.0, so an absolute
    relevance gate runs first: a chunk needs raw cosine >= min_cos (or, without
    an embedder, >= min_lex raw query-word hits). Title-matched / broad-KB
    queries skip the gate. Each result carries its raw cosine as "cos".
    """
    if not allowed_knowledge_source_ids:
        return []

    # Check titles of allowed knowledge sources from auth_db
    source_titles = {}
    try:
        from .auth_db import list_knowledge_sources
        for ks in list_knowledge_sources():
            if ks["id"] in allowed_knowledge_source_ids and ks["status"] == "ready":
                source_titles[ks["id"]] = ks["title"]
    except Exception as e:
        print(f"[memory] failed reading knowledge source titles: {e}", file=sys.stderr)

    cache = _cached_entries()
    # Pre-filter by allowed source IDs
    idxs, entries = [], []
    for i, (source, path, text, v) in enumerate(cache["entries"]):
        if source != "knowledge":
            continue
        kid = _knowledge_id_from_path(path)
        if kid is not None and kid in allowed_knowledge_source_ids:
            idxs.append(i)
            entries.append((source, path, text, v, kid))
    if not entries:
        return []

    qv = await _embed_texts([query])
    qvec = qv[0] if qv else None

    q_lower = query.lower()
    words = [w.lower() for w in re.findall(r"\w{3,}", query)][:10]

    # Detect if query explicitly matches source titles or is a broad company inquiry
    # whole words only: a substring test lets "online" in a title match any
    # "online ..." query and bypass the relevance gate below
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
    cos_raw = _cosines(cache, idxs, qvec)
    lower = cache["lower"]
    for j, (_source, _path, text, v, kid) in enumerate(entries):
        tl = lower[idxs[j]]
        lex = float(sum(tl.count(w) for w in words))
        query_lex.append(lex)
        # Extra lexical boost if source title words appear in text
        if kid in source_titles:
            s_title = source_titles[kid].lower()
            lex += float(sum(tl.count(tw) for tw in re.findall(r"\w{3,}", s_title)))
        lex_raw.append(lex)

        # Title / broad boost
        boost = 0.0
        if kid in matched_title_kids:
            boost += 0.35
        elif is_broad_kb:
            boost += 0.20
        title_boost.append(boost)

    lex_n = _norm(lex_raw)
    cos_n = _norm(cos_raw) if qvec is not None else [0.0] * len(entries)

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



async def ensure_indexed(max_age_s: float = 900) -> None:
    """Index if the store is older than max_age_s (locked, non-throwing)."""
    global _last_index
    if time.time() - _last_index < max_age_s:
        return
    async with _index_lock:
        if time.time() - _last_index < max_age_s:
            return
        await index_workspace()
        await index_sessions()
        _last_index = time.time()


async def memory_background_task():
    """Boot-time background indexing; refreshes every 15 minutes."""
    await asyncio.sleep(6)  # let the server finish booting first
    while True:
        try:
            await ensure_indexed(max_age_s=0)
        except Exception as e:
            print(f"[memory] background index error: {e}", file=sys.stderr)
        await asyncio.sleep(900)


