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


def set_source_cloud_ok(source_id: int, ok: bool) -> None:
    db().execute("UPDATE knowledge_sources SET cloud_ok = ?, updated_at = ? WHERE id = ?",
                 (1 if ok else 0, time.time(), source_id))
    db().commit()


def cloud_ok_source_ids() -> set:
    """Sources cloud models may read: the category's setting when the source has a (still existing)
    category, otherwise the source's own switch."""
    rows = db().execute(
        "SELECT s.id FROM knowledge_sources s LEFT JOIN knowledge_categories c ON c.name = s.category "
        "WHERE (c.name IS NOT NULL AND c.cloud_ok = 1) OR (c.name IS NULL AND s.cloud_ok = 1)").fetchall()
    return {r["id"] for r in rows}


def set_source_category(source_id: int, category: Optional[str]) -> None:
    db().execute("UPDATE knowledge_sources SET category = ?, updated_at = ? WHERE id = ?",
                 (category or None, time.time(), source_id))
    db().commit()


def list_knowledge_categories() -> list:
    return [dict(r) for r in db().execute(
        "SELECT c.name, c.cloud_ok, (SELECT COUNT(*) FROM knowledge_sources s WHERE s.category = c.name) AS sources "
        "FROM knowledge_categories c ORDER BY c.created_at, c.name").fetchall()]


def upsert_knowledge_category(name: str, cloud_ok: bool) -> None:
    db().execute("INSERT INTO knowledge_categories (name, cloud_ok, created_at) VALUES (?, ?, ?) "
                 "ON CONFLICT(name) DO UPDATE SET cloud_ok = excluded.cloud_ok",
                 (name, 1 if cloud_ok else 0, time.time()))
    db().commit()


def delete_knowledge_category(name: str) -> None:
    """Sources filed under it go back to uncategorised (their own cloud switch applies)."""
    db().execute("UPDATE knowledge_sources SET category = NULL WHERE category = ?", (name,))
    db().execute("DELETE FROM knowledge_categories WHERE name = ?", (name,))
    db().commit()


def list_knowledge_rules(enabled_only: bool = False) -> list:
    q = "SELECT * FROM knowledge_rules" + (" WHERE enabled = 1" if enabled_only else "") + " ORDER BY id"
    return [dict(r) for r in db().execute(q).fetchall()]


def add_knowledge_rule(name: str, kind: str, pattern: str) -> int:
    cur = db().execute("INSERT INTO knowledge_rules (name, kind, pattern, enabled, builtin, created_at) "
                       "VALUES (?, ?, ?, 1, 0, ?)", (name, kind, pattern, time.time()))
    db().commit()
    return cur.lastrowid


def set_knowledge_rule_enabled(rule_id: int, enabled: bool) -> bool:
    cur = db().execute("UPDATE knowledge_rules SET enabled = ? WHERE id = ?", (1 if enabled else 0, rule_id))
    db().commit()
    return cur.rowcount > 0


def delete_knowledge_rule(rule_id: int) -> bool:
    cur = db().execute("DELETE FROM knowledge_rules WHERE id = ? AND builtin = 0", (rule_id,))
    db().commit()
    return cur.rowcount > 0


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
