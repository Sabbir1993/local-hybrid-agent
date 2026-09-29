import json
import sqlite3
import sys
from typing import Optional

from ..config import MEMORY_DB_FILE
from ..sqlite_util import ThreadLocalDB

try:
    import numpy as _np
except Exception:
    _np = None

_conn: Optional[ThreadLocalDB] = None


def _db() -> ThreadLocalDB:
    global _conn
    pkg = sys.modules.get("core.memory")
    conn = getattr(pkg, "_conn", None) if pkg else _conn
    if conn is None:
        db_file = getattr(pkg, "MEMORY_DB_FILE", MEMORY_DB_FILE) if pkg else MEMORY_DB_FILE
        conn = ThreadLocalDB(db_file)
        conn.execute("""
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
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('chunks_gen', '0')")
        for ev in ("INSERT", "UPDATE", "DELETE"):
            conn.execute(f"""CREATE TRIGGER IF NOT EXISTS chunks_gen_{ev.lower()} AFTER {ev} ON chunks
                BEGIN UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'chunks_gen'; END""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_novec ON chunks(source) WHERE vec IS NULL")
        conn.commit()
        if pkg:
            pkg._conn = conn
        _conn = conn
    return conn


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
    """Danger-zone: drop indexed chat/workspace memory."""
    _db().execute("DELETE FROM chunks WHERE source IN ('workspace', 'session')")
    _db().commit()
