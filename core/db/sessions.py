import json
import time
from typing import Optional

from ..sqlite_util import transaction
from .common import _forget_session_memory, _get_projects_db, _projects_db
from .projects import db_project_owner, db_session_owner


def _at_rest(text):
    """Mask card numbers before anything lands in projects.db (PCI DSS 3.4)."""
    from .. import pan
    if not isinstance(text, str) or not pan.enabled("pan_at_rest"):
        return text
    return pan.mask_pans(text)[0]


def _meta_at_rest(obj):
    """Mask PANs in a meta dict's string values only."""
    if isinstance(obj, str):
        return _at_rest(obj)
    if isinstance(obj, dict):
        return {k: _meta_at_rest(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_meta_at_rest(v) for v in obj]
    return obj


def _load_meta(raw):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def db_list_sessions_page(pid: Optional[int], owner_user_id: int, limit: Optional[int] = None,
                          before_id: Optional[int] = None) -> tuple:
    """(sessions, has_more): newest first; `before_id` continues after the last id of the previous page."""
    if pid is None or pid == 0:
        where, args = "project_id IS NULL AND user_id = ?", [owner_user_id]
    else:
        if db_project_owner(pid) != owner_user_id:
            raise PermissionError("not your project")
        where, args = "project_id = ? AND user_id = ?", [pid, owner_user_id]
    if before_id is not None:
        where += " AND id < ?"
        args.append(int(before_id))
    sql = f"SELECT * FROM sessions WHERE {where} ORDER BY id DESC"
    if limit:
        sql += " LIMIT ?"
        args.append(int(limit) + 1)       # one extra row tells whether another page exists
    rows = list(_projects_db.execute(sql, tuple(args)))
    has_more = bool(limit) and len(rows) > int(limit)
    if has_more:
        rows = rows[:int(limit)]
    return ([{"id": r["id"], "title": r["title"], "created_at": r["created_at"]} for r in rows], has_more)


def db_list_sessions(pid: Optional[int], owner_user_id: int) -> list:
    return db_list_sessions_page(pid, owner_user_id)[0]


def db_create_session(pid: Optional[int], title: str = None, owner_user_id: int = None) -> dict:
    if owner_user_id is None:
        raise ValueError("owner_user_id required")
    now = time.time()
    title = _at_rest((title or "New session").strip())[:80]
    actual_pid = None if (pid is None or pid == 0) else pid
    if actual_pid is not None and db_project_owner(actual_pid) != owner_user_id:
        raise PermissionError("not your project")
    cur = _projects_db.execute(
        "INSERT INTO sessions (project_id, title, created_at, user_id) VALUES (?, ?, ?, ?)",
        (actual_pid, title, now, owner_user_id))
    _projects_db.commit()
    return {"id": cur.lastrowid, "title": title, "created_at": now}


def db_update_session_title(sid: int, title: str, owner_user_id: int) -> None:
    if db_session_owner(sid) != owner_user_id:
        raise PermissionError("not your session")
    title = _at_rest((title or "New session").strip())[:80]
    _projects_db.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, sid))
    _projects_db.commit()


def db_delete_session(sid: int, owner_user_id: int) -> None:
    if db_session_owner(sid) != owner_user_id:
        raise PermissionError("not your session")
    pdb = _get_projects_db()
    with transaction(pdb) as c:
        c.execute("DELETE FROM plan_items WHERE session_id = ?", (sid,))
        c.execute("DELETE FROM session_working_memory WHERE session_id = ?", (sid,))
        c.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
        c.execute("DELETE FROM sessions WHERE id = ?", (sid,))
    _forget_session_memory([sid])


def db_load_messages(sid: int, owner_user_id: int) -> list:
    if db_session_owner(sid) != owner_user_id:
        raise PermissionError("not your session")
    return [
        {"role": r["role"], "content": r["content"], "meta": _load_meta(r["meta"])}
        for r in _projects_db.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY id", (sid,))
    ]


def db_append_message(sid: int, role: str, content: str, meta: dict = None, owner_user_id: int = None) -> int:
    if owner_user_id is None or db_session_owner(sid) != owner_user_id:
        raise PermissionError("not your session")
    cur = _projects_db.execute(
        "INSERT INTO messages (session_id, role, content, meta, created_at) VALUES (?, ?, ?, ?, ?)",
        (sid, role, _at_rest(content), json.dumps(_meta_at_rest(meta)) if meta else None, time.time()))
    _projects_db.commit()
    return cur.lastrowid


# ---------------- structured plan tracking ----------------
_VALID_PLAN_STATUS = ("pending", "in_progress", "done", "failed")


def db_set_plan_items(sid: int, texts: list) -> list:
    """Replace the session's plan with an ordered list of step texts (status reset to pending)."""
    now = time.time()
    with transaction(_projects_db) as c:
        c.execute("DELETE FROM plan_items WHERE session_id = ?", (sid,))
        c.executemany(
            "INSERT INTO plan_items (session_id, ord, text, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'pending', ?, ?)",
            [(sid, i, str(t)[:1000], now, now) for i, t in enumerate(texts, start=1)])
    return db_get_plan_items(sid)


def db_get_plan_items(sid: int) -> list:
    if sid is None:
        return []
    return [
        {"id": r["id"], "ord": r["ord"], "text": r["text"],
         "status": r["status"], "note": r["note"]}
        for r in _projects_db.execute(
            "SELECT * FROM plan_items WHERE session_id = ? ORDER BY ord", (sid,))
    ]


def db_set_plan_item_status(sid: int, ord_no: int, status: str, note: Optional[str] = None) -> Optional[dict]:
    """Update one plan item by its 1-based step number. Returns the updated row or None."""
    if status not in _VALID_PLAN_STATUS:
        raise ValueError(f"invalid plan status '{status}'")
    cur = _projects_db.execute(
        "UPDATE plan_items SET status = ?, note = COALESCE(?, note), updated_at = ? "
        "WHERE session_id = ? AND ord = ?",
        (status, note, time.time(), sid, int(ord_no)))
    _projects_db.commit()
    if cur.rowcount <= 0:
        return None
    row = _projects_db.execute(
        "SELECT * FROM plan_items WHERE session_id = ? AND ord = ?", (sid, int(ord_no))).fetchone()
    if row is None:
        return None
    return {"id": row["id"], "ord": row["ord"], "text": row["text"],
            "status": row["status"], "note": row["note"]}
