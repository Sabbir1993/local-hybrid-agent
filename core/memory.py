"""
core/memory.py - Real semantic memory for the A770 runtime.

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
import time
from pathlib import Path
from typing import Optional

from .config import MEMORY_DB_FILE

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


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(MEMORY_DB_FILE), check_same_thread=False)
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


def _workspace_dir() -> Optional[Path]:
    try:
        from .agent_tools import active_workspace
        return active_workspace()
    except Exception:
        try:
            from .small_model import WORKSPACE_ROOT
            return WORKSPACE_ROOT
        except Exception:
            return None


async def index_workspace(ws_path: Optional[Path] = None, force: bool = False) -> dict:
    """Incrementally index workspace files into the vector store."""
    ws = Path(ws_path) if ws_path else _workspace_dir()
    if not ws or not ws.exists():
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


def _load_entries() -> list:
    out = []
    for source, path, text, vec in _db().execute(
            "SELECT source, path, text, vec FROM chunks").fetchall():
        v = None
        if vec is not None:
            try:
                raw = bytes(vec)
                if _np is not None:
                    v = _np.frombuffer(raw, dtype=_np.float32)
                else:
                    v = json.loads(raw.decode())
            except Exception:
                v = None
        out.append((source, path, text, v))
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
                                allowed_knowledge_source_ids: Optional[set] = None) -> list:
    """Hybrid recall: 0.5*cosine + 0.5*lexical (lexical-only without embedder).

    Strict per-user isolation: "session"-sourced chunks (past chat titles/first
    messages) are filtered to the requesting user's own sessions only -- other
    users' chat history must never surface via memory search, even indirectly.
    Workspace chunks are unaffected (shared project context).

    "knowledge"-sourced chunks (org knowledge base) are filtered to
    allowed_knowledge_source_ids *before* scoring -- an unpermitted caller's
    query never sees a restricted document as a retrieval candidate at all,
    which is what makes silence (no hit) safe instead of a leaky special case.
    When allowed_knowledge_source_ids is None, all knowledge chunks are
    excluded (callers must pass an explicit set, even if empty, to opt in).
    """
    entries = _load_entries()
    if not entries:
        return []
    if requesting_user_id is not None or allowed_knowledge_source_ids is not None:
        from .db import db_session_owner
        filtered = []
        for source, path, text, v in entries:
            if source == "session" and requesting_user_id is not None:
                sid = _session_id_from_path(path)
                owner = db_session_owner(sid) if sid is not None else None
                if owner is not None and owner != requesting_user_id:
                    continue
            if source == "knowledge":
                if not allowed_knowledge_source_ids:
                    continue
                kid = _knowledge_id_from_path(path)
                if kid is None or kid not in allowed_knowledge_source_ids:
                    continue
            filtered.append((source, path, text, v))
        entries = filtered
    else:
        entries = [e for e in entries if e[0] != "knowledge"]
    if not entries:
        return []
    qv = await _embed_texts([query])
    qvec = qv[0] if qv else None

    words = [w.lower() for w in re.findall(r"\w{3,}", query)][:8]
    lex_raw, cos_raw = [], []
    for _source, _path, text, v in entries:
        tl = text.lower()
        lex_raw.append(float(sum(tl.count(w) for w in words)))
        if qvec is not None and v is not None:
            if _np is not None:
                a = _np.asarray(qvec, dtype=_np.float32)
                b = _np.asarray(v, dtype=_np.float32)
                denom = float(_np.linalg.norm(a) * _np.linalg.norm(b))
                cos_raw.append(float(_np.dot(a, b) / denom) if denom > 1e-9 else 0.0)
            else:
                dot = sum(x * y for x, y in zip(qvec, v))
                na = sum(x * x for x in qvec) ** 0.5
                nb = sum(y * y for y in v) ** 0.5
                cos_raw.append(dot / (na * nb) if na * nb > 1e-9 else 0.0)
        else:
            cos_raw.append(0.0)

    lex_n = _norm(lex_raw)
    cos_n = _norm(cos_raw) if qvec is not None else None
    results = []
    for i, (source, path, text, _v) in enumerate(entries):
        score = (0.5 * cos_n[i] + 0.5 * lex_n[i]) if cos_n is not None else lex_n[i]
        results.append({"source": source, "path": path, "text": text, "score": score})
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


