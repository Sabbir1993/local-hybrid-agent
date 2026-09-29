import sqlite3
import time
from typing import Optional

from .common import db


def create_knowledge_source(title: str, kind: str, origin: Optional[str] = None,
                            stored_path: Optional[str] = None, content_hash: Optional[str] = None,
                            status: str = "pending", created_by: Optional[int] = None) -> int:
    now = time.time()
    cur = db().execute(
        "INSERT INTO knowledge_sources (title, kind, origin, stored_path, content_hash, status, "
        "created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (title, kind, origin, stored_path, content_hash, status, created_by, now, now),
    )
    db().commit()
    return cur.lastrowid


def update_knowledge_source_status(source_id: int, status: str, error: Optional[str] = None) -> None:
    db().execute("UPDATE knowledge_sources SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                 (status, error, time.time(), source_id))
    db().commit()


def list_knowledge_sources() -> list:
    return db().execute("SELECT * FROM knowledge_sources ORDER BY created_at DESC").fetchall()


def get_knowledge_source(source_id: int) -> Optional[sqlite3.Row]:
    return db().execute("SELECT * FROM knowledge_sources WHERE id = ?", (source_id,)).fetchone()


def delete_knowledge_source(source_id: int) -> None:
    db().execute("DELETE FROM knowledge_sources WHERE id = ?", (source_id,))
    db().commit()


def get_source_role_names(source_id: int) -> list:
    rows = db().execute(
        "SELECT r.name FROM knowledge_source_role_access ksra JOIN roles r ON r.id = ksra.role_id "
        "WHERE ksra.source_id = ?", (source_id,),
    ).fetchall()
    return [r["name"] for r in rows]


def set_source_role_access(source_id: int, role_names: list, granted_by: Optional[int] = None) -> None:
    now = time.time()
    db().execute("DELETE FROM knowledge_source_role_access WHERE source_id = ?", (source_id,))
    for name in role_names:
        role = db().execute("SELECT id FROM roles WHERE name = ?", (name,)).fetchone()
        if role:
            db().execute(
                "INSERT OR IGNORE INTO knowledge_source_role_access "
                "(source_id, role_id, granted_at, granted_by) VALUES (?, ?, ?, ?)",
                (source_id, role["id"], now, granted_by),
            )
    db().commit()


def allowed_knowledge_source_ids_for_roles(role_names: list) -> set:
    # A source with zero rows in knowledge_source_role_access has no roles
    # assigned - the admin UI treats leaving the role picker empty as "allow
    # all users", so such sources are public rather than accessible to no one.
    public_rows = db().execute(
        "SELECT s.id FROM knowledge_sources s WHERE NOT EXISTS "
        "(SELECT 1 FROM knowledge_source_role_access ksra WHERE ksra.source_id = s.id)"
    ).fetchall()
    ids = {r["id"] for r in public_rows}
    if role_names:
        placeholders = ",".join("?" * len(role_names))
        rows = db().execute(
            f"SELECT DISTINCT ksra.source_id FROM knowledge_source_role_access ksra "
            f"JOIN roles r ON r.id = ksra.role_id WHERE r.name IN ({placeholders})",
            role_names,
        ).fetchall()
        ids |= {r["source_id"] for r in rows}
    return ids
