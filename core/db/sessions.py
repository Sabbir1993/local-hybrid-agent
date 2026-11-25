import json
import secrets
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


def _clean_model_name(m: Optional[str]) -> Optional[str]:
    if not m:
        return None
    s = str(m).strip()
    if not s:
        return None
    if ("\\" in s) or ("/" in s):
        import os
        s = os.path.basename(s.replace("\\", "/"))
    if s.lower().endswith(".gguf"):
        s = s[:-5]
    return s.strip()[:200] or None


def _has_table(table: str) -> bool:
    try:
        row = _projects_db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        return bool(row)
    except Exception:
        return False


def db_list_sessions_page(pid: Optional[int], owner_user_id: int, limit: Optional[int] = None,
                          before_id: Optional[int] = None) -> tuple:
    """(sessions, has_more): newest activity first; `before_id` continues after the last id of the previous page."""
    if pid is None or pid == 0:
        where, args = "project_id IS NULL AND user_id = ?", [owner_user_id]
    else:
        if db_project_owner(pid) != owner_user_id:
            raise PermissionError("not your project")
        where, args = "project_id = ? AND user_id = ?", [pid, owner_user_id]
    if before_id is not None:
        where += " AND id < ?"
        args.append(int(before_id))
    last_at_expr = "COALESCE((SELECT MAX(m.created_at) FROM messages m WHERE m.session_id = s.id), s.created_at) AS last_at" if _has_table("messages") else "s.created_at AS last_at"
    sql = f"""SELECT s.*, {last_at_expr}
              FROM sessions s WHERE {where} ORDER BY last_at DESC, s.id DESC"""
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
        "last_at": r["last_at"],
        "agent_id": r["agent_id"] if "agent_id" in r.keys() else None,
        "project_id": r["project_id"] if "project_id" in r.keys() else None,
        "model": _clean_model_name(r["model"]) if "model" in r.keys() else None,
        "is_pinned": bool(r["is_pinned"]) if "is_pinned" in r.keys() and r["is_pinned"] else False,
    } for r in rows], has_more)


def db_list_sessions(pid: Optional[int], owner_user_id: int) -> list:
    return db_list_sessions_page(pid, owner_user_id)[0]


def db_create_session(pid: Optional[int], title: str = None, owner_user_id: int = None, agent_id: Optional[int] = None,
                      model: Optional[str] = None) -> dict:
    if owner_user_id is None:
        raise ValueError("owner_user_id required")
    now = time.time()
    title = _at_rest((title or "New session").strip())[:80]
    actual_pid = None if (pid is None or pid == 0) else pid
    if actual_pid is not None and db_project_owner(actual_pid) != owner_user_id:
        raise PermissionError("not your project")
    cur = _projects_db.execute(
        "INSERT INTO sessions (project_id, title, created_at, user_id, agent_id, model) VALUES (?, ?, ?, ?, ?, ?)",
        (actual_pid, title, now, owner_user_id, agent_id, _clean_model_name(model)))
    _projects_db.commit()
    return {"id": cur.lastrowid, "title": title, "created_at": now, "agent_id": agent_id, "model": _clean_model_name(model)}


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


def db_update_session_model(sid: int, model: Optional[str], owner_user_id: int) -> None:
    if db_session_owner(sid) != owner_user_id:
        raise PermissionError("not your session")
    _projects_db.execute("UPDATE sessions SET model = ? WHERE id = ?", (_clean_model_name(model), sid))
    _projects_db.commit()


def _has_pinned_column() -> bool:
    try:
        cols = [col[1] for col in _projects_db.execute("PRAGMA table_info(sessions)").fetchall()]
        return "is_pinned" in cols
    except Exception:
        return False


def db_update_session_pinned(sid: int, is_pinned: bool, owner_user_id: int) -> None:
    if db_session_owner(sid) != owner_user_id:
        raise PermissionError("not your session")
    _projects_db.execute("UPDATE sessions SET is_pinned = ? WHERE id = ?", (1 if is_pinned else 0, sid))
    _projects_db.commit()


def db_search_sessions(owner_user_id: int, query: str, limit: int = 50) -> list:
    """Search sessions by matching query in title OR message contents."""
    q_str = (query or "").strip()
    if not q_str:
        return []
    needle = f"%{q_str}%"
    pin_col = "s.is_pinned" if _has_pinned_column() else "0 AS is_pinned"
    pin_order = "s.is_pinned DESC," if _has_pinned_column() else ""
    sql = f"""
        SELECT DISTINCT s.id, s.title, s.created_at, s.agent_id, s.model, {pin_col}, s.project_id,
               COALESCE((SELECT MAX(m.created_at) FROM messages m WHERE m.session_id = s.id), s.created_at) AS last_at
        FROM sessions s
        LEFT JOIN messages m ON m.session_id = s.id
        WHERE s.user_id = ? AND (s.title LIKE ? OR m.content LIKE ?)
        ORDER BY {pin_order} last_at DESC
        LIMIT ?
    """
    rows = list(_projects_db.execute(sql, (owner_user_id, needle, needle, limit)))
    return [{
        "id": r["id"],
        "title": r["title"],
        "created_at": r["created_at"],
        "last_at": r["last_at"],
        "agent_id": r["agent_id"] if "agent_id" in r.keys() else None,
        "project_id": r["project_id"] if "project_id" in r.keys() else None,
        "model": _clean_model_name(r["model"]) if "model" in r.keys() else None,
        "is_pinned": bool(r["is_pinned"]) if "is_pinned" in r.keys() and r["is_pinned"] else False,
    } for r in rows]



def db_delete_session(sid: int, owner_user_id: int, is_super_admin: bool = False) -> None:
    owner = db_session_owner(sid)
    if not is_super_admin and owner is not None and owner != owner_user_id:
        raise PermissionError("not your session")
    pdb = _get_projects_db()
    with transaction(pdb) as c:
        c.execute("DELETE FROM plan_items WHERE session_id = ?", (sid,))
        c.execute("DELETE FROM session_working_memory WHERE session_id = ?", (sid,))
        c.execute("DELETE FROM session_research WHERE session_id = ?", (sid,))
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


def db_session_groups(owner_user_id: int, kind: str, group_ids: list, per_group: int = 5) -> dict:
    """The sidebar tree: {group_id: {"total": n, "sessions": [...newest activity first, at most per_group]}}.

    kind 'agent' groups the user's agent chats (no project) by agent_id; kind 'project' groups the user's project
    tasks by project_id. Only rows of owner_user_id are read, so a foreign id in group_ids just comes back empty.
    A session's last activity is its newest message, or its creation time when it has none."""
    ids = sorted({int(i) for i in group_ids or [] if str(i).strip().lstrip("-").isdigit()})[:200]
    out = {str(i): {"total": 0, "sessions": []} for i in ids}
    if not ids:
        return out
    col = "agent_id" if kind == "agent" else "project_id"
    extra = " AND s.project_id IS NULL" if kind == "agent" else ""
    marks = ",".join("?" for _ in ids)
    pin_col = "s.is_pinned" if _has_pinned_column() else "0 AS is_pinned"
    rows = _projects_db.execute(
        f"""SELECT s.id, s.title, s.created_at, s.agent_id, s.project_id, s.model, {pin_col},
                   COALESCE((SELECT MAX(m.created_at) FROM messages m WHERE m.session_id = s.id), s.created_at) AS last_at
            FROM sessions s WHERE s.user_id = ? AND s.{col} IN ({marks}){extra}""",
        (owner_user_id, *ids))
    per = max(1, min(int(per_group or 5), 500))
    buckets: dict = {}
    for r in rows:
        buckets.setdefault(str(r[col]), []).append(r)
    for gid, rs in buckets.items():
        rs.sort(key=lambda r: (r["is_pinned"] or 0, r["last_at"] or 0, r["id"]), reverse=True)
        out[gid] = {"total": len(rs), "sessions": [{
            "id": r["id"], "title": r["title"], "created_at": r["created_at"], "last_at": r["last_at"],
            "agent_id": r["agent_id"], "project_id": r["project_id"], "model": _clean_model_name(r["model"]),
            "is_pinned": bool(r["is_pinned"]) if "is_pinned" in r.keys() and r["is_pinned"] else False,
        } for r in rs[:per]]}
    return out


def db_create_or_get_share(sid: int, owner_user_id: int, password: Optional[str] = None) -> str:
    """Return an existing share_key or create a new cryptographic share key for a session owned by owner_user_id."""
    if owner_user_id is None or db_session_owner(sid) != owner_user_id:
        raise PermissionError("not your session")
    pw_hash: Optional[str] = None
    if password:
        from core.auth_provider import hash_password
        pw_hash = hash_password(password)
    row = _projects_db.execute("SELECT share_key FROM shared_sessions WHERE session_id = ?", (sid,)).fetchone()
    if row:
        if pw_hash is not None:
            with transaction(_projects_db):
                _projects_db.execute(
                    "UPDATE shared_sessions SET password_hash = ? WHERE session_id = ?",
                    (pw_hash, sid),
                )
        return row["share_key"]
    share_key = secrets.token_urlsafe(24)
    with transaction(_projects_db):
        _projects_db.execute(
            "INSERT INTO shared_sessions (session_id, share_key, created_at, user_id, password_hash) VALUES (?, ?, ?, ?, ?)",
            (sid, share_key, time.time(), owner_user_id, pw_hash),
        )
    return share_key


def db_get_share_auth_info(share_key: str) -> Optional[dict]:
    """Return lightweight auth metadata for a share_key without loading messages."""
    if not share_key or not isinstance(share_key, str):
        return None
    row = _projects_db.execute(
        "SELECT s.id, s.title, sh.password_hash "
        "FROM shared_sessions sh "
        "JOIN sessions s ON s.id = sh.session_id "
        "WHERE sh.share_key = ?",
        (share_key.strip(),),
    ).fetchone()
    if not row:
        return None
    return {
        "session_id": row["id"],
        "title": row["title"],
        "password_hash": row["password_hash"],
        "password_required": bool(row["password_hash"]),
    }


def db_verify_share_password(share_key: str, password: str) -> bool:
    """Verify a visitor-supplied password against the stored share password hash."""
    info = db_get_share_auth_info(share_key)
    if not info:
        return False
    if not info["password_required"]:
        return True
    from core.auth_provider import verify_password
    return verify_password(password or "", info["password_hash"])


def db_get_shared_session(share_key: str) -> Optional[dict]:
    """Retrieve shared session info and messages by share_key (no auth needed)."""
    if not share_key or not isinstance(share_key, str):
        return None
    row = _projects_db.execute(
        "SELECT s.id, s.title, s.created_at, s.user_id, sh.created_at as shared_at "
        "FROM shared_sessions sh "
        "JOIN sessions s ON s.id = sh.session_id "
        "WHERE sh.share_key = ?",
        (share_key.strip(),),
    ).fetchone()
    if not row:
        return None
    sid = row["id"]
    raw_msgs = _projects_db.execute(
        "SELECT role, content, meta, created_at FROM messages WHERE session_id = ? ORDER BY id",
        (sid,),
    ).fetchall()
    clean_msgs = []
    for m in raw_msgs:
        role = m["role"]
        if role not in ("user", "assistant"):
            continue
        content = m["content"] or ""
        # Skip empty assistant messages (e.g. intermediate tool-only calls)
        if role == "assistant" and not content.strip():
            continue
        meta = _load_meta(m["meta"])
        clean_meta = {}
        if isinstance(meta, dict) and role == "user":
            for k in ("displayContent", "images", "files", "timeStr"):
                if k in meta:
                    clean_meta[k] = meta[k]
        clean_msgs.append({
            "role": role,
            "content": content,
            "meta": clean_meta,
            "created_at": m["created_at"],
            "thinking": meta.get("reasoning", "") if isinstance(meta, dict) else "",
        })
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "title": row["title"],
        "created_at": row["created_at"],
        "shared_at": row["shared_at"],
        "messages": clean_msgs,
    }


def db_revoke_share(sid: int, owner_user_id: int) -> bool:
    """Revoke/delete an existing share link for a session."""
    if owner_user_id is None or db_session_owner(sid) != owner_user_id:
        raise PermissionError("not your session")
    with transaction(_projects_db):
        cur = _projects_db.execute("DELETE FROM shared_sessions WHERE session_id = ?", (sid,))
    return cur.rowcount > 0
