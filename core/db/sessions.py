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
    return ([{
        "id": r["id"],
        "title": r["title"],
        "created_at": r["created_at"],
        "agent_id": r["agent_id"] if "agent_id" in r.keys() else None,
    } for r in rows], has_more)


def db_list_sessions(pid: Optional[int], owner_user_id: int) -> list:
    return db_list_sessions_page(pid, owner_user_id)[0]


def db_create_session(pid: Optional[int], title: str = None, owner_user_id: int = None, agent_id: Optional[int] = None) -> dict:
    if owner_user_id is None:
        raise ValueError("owner_user_id required")
    now = time.time()
    title = _at_rest((title or "New session").strip())[:80]
    actual_pid = None if (pid is None or pid == 0) else pid
    if actual_pid is not None and db_project_owner(actual_pid) != owner_user_id:
        raise PermissionError("not your project")
    cur = _projects_db.execute(
        "INSERT INTO sessions (project_id, title, created_at, user_id, agent_id) VALUES (?, ?, ?, ?, ?)",
        (actual_pid, title, now, owner_user_id, agent_id))
    _projects_db.commit()
    return {"id": cur.lastrowid, "title": title, "created_at": now, "agent_id": agent_id}


def db_update_session_title(sid: int, title: str, owner_user_id: int) -> None:
    if db_session_owner(sid) != owner_user_id:
        raise PermissionError("not your session")
    title = _at_rest((title or "New session").strip())[:80]
    _projects_db.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, sid))
    _projects_db.commit()


def db_update_session_agent_id(sid: int, agent_id: Optional[int], owner_user_id: int) -> None:
    if db_session_owner(sid) != owner_user_id:
        raise PermissionError("not your session")
    _projects_db.execute("UPDATE sessions SET agent_id = ? WHERE id = ?", (agent_id, sid))
    _projects_db.commit()


def db_delete_session(sid: int, owner_user_id: int, is_super_admin: bool = False) -> None:
    owner = db_session_owner(sid)
    if not is_super_admin and owner is not None and owner != owner_user_id:
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
        {"role": r["role"], "content": r["content"], "meta": _load_meta(r["meta"]), "created_at": r["created_at"]}
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


def db_fork_session(sid: int, owner_user_id: int, upto_message_id: Optional[int] = None) -> dict:
    """Fork a session at (and including) message `upto_message_id`, or copy all of it.

    Branch, not truncation: the source session is untouched, so a resume/rewrite attempt
    from an earlier point can never lose the original history. Plan items (statuses included,
    so a retry resumes exactly where the plan stands) and the session's working memory are
    carried over; ids are re-issued. The new session keeps project_id/user_id/agent_id and
    records `fork_of`, its title is the parent's prefixed with an arrow (still <=80 chars,
    PAN-masked like every title).

    Raises PermissionError when the caller does not own the session, ValueError when
    `upto_message_id` is not a message of this session (the caller asked to fork at a
    message that does not exist - saying so loudly beats silently forking everything)."""
    if owner_user_id is None or db_session_owner(sid) != owner_user_id:
        raise PermissionError("not your session")
    src = _projects_db.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
    if src is None:
        raise PermissionError("not your session")
    upto_sql, upto_args = "", ()
    if upto_message_id is not None:
        has = _projects_db.execute("SELECT 1 FROM messages WHERE id = ? AND session_id = ?",
                                   (int(upto_message_id), sid)).fetchone()
        if has is None:
            raise ValueError("message not in this session")
        upto_sql, upto_args = " AND id <= ?", (int(upto_message_id),)
    title = _at_rest("↳ " + str(src["title"]))[:80]
    with transaction(_get_projects_db()) as c:
        cur = c.execute(
            "INSERT INTO sessions (project_id, title, created_at, user_id, agent_id, fork_of) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (src["project_id"], title, time.time(), src["user_id"], src["agent_id"], sid))
        new_id = cur.lastrowid
        c.execute(
            f"INSERT INTO messages (session_id, role, content, meta, created_at) "
            f"SELECT ?, role, content, meta, created_at FROM messages "
            f"WHERE session_id = ?{upto_sql}",
            (new_id, sid, *upto_args))
        c.execute(
            "INSERT INTO plan_items (session_id, ord, text, status, deps, phase, verification, created_at, updated_at) "
            "SELECT ?, ord, text, status, deps, phase, verification, ?, ? FROM plan_items WHERE session_id = ?",
            (new_id, time.time(), time.time(), sid))
        c.execute(
            "INSERT INTO session_working_memory (session_id, user_id, content, updated_at) "
            "SELECT ?, user_id, content, ? FROM session_working_memory WHERE session_id = ?",
            (new_id, time.time(), sid))
    n_messages = _projects_db.execute("SELECT COUNT(*) FROM messages WHERE session_id = ?", (new_id,)).fetchone()[0]
    return {"id": new_id, "title": title, "fork_of": sid, "messages": n_messages}


# ---------------- structured plan tracking ----------------
_VALID_PLAN_STATUS = ("pending", "in_progress", "done", "failed")


def db_set_plan_items(sid: int, items_data: list) -> list:
    """Replace the session's plan with an ordered list of step texts or DAG dicts (status reset to pending)."""
    now = time.time()
    rows = []
    for i, it in enumerate(items_data, start=1):
        if isinstance(it, dict):
            text = str(it.get("text") or it.get("title") or "").strip()[:1000]
            deps = json.dumps(it.get("deps") or [])
            phase = it.get("phase")
            verification = it.get("verification")
        else:
            text = str(it)[:1000]
            deps = "[]"
            phase = None
            verification = None
        rows.append((sid, i, text, "pending", deps, phase, verification, now, now))
    with transaction(_projects_db) as c:
        c.execute("DELETE FROM plan_items WHERE session_id = ?", (sid,))
        c.executemany(
            "INSERT INTO plan_items (session_id, ord, text, status, deps, phase, verification, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows)
    return db_get_plan_items(sid)


def db_get_plan_items(sid: int) -> list:
    if sid is None:
        return []
    out = []
    for r in _projects_db.execute(
        "SELECT * FROM plan_items WHERE session_id = ? ORDER BY ord", (sid,)):
        d = {
            "id": r["id"], "ord": r["ord"], "text": r["text"],
            "status": r["status"], "note": r["note"]
        }
        if "deps" in r.keys() and r["deps"]:
            try:
                d["deps"] = json.loads(r["deps"])
            except Exception:
                d["deps"] = []
        else:
            d["deps"] = []
        if "phase" in r.keys():
            d["phase"] = r["phase"]
        if "verification" in r.keys():
            d["verification"] = r["verification"]
        out.append(d)
    return out


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
    d = {"id": row["id"], "ord": row["ord"], "text": row["text"],
         "status": row["status"], "note": row["note"]}
    if "deps" in row.keys() and row["deps"]:
        try:
            d["deps"] = json.loads(row["deps"])
        except Exception:
            d["deps"] = []
    else:
        d["deps"] = []
    if "phase" in row.keys():
        d["phase"] = row["phase"]
    if "verification" in row.keys():
        d["verification"] = row["verification"]
    return d
