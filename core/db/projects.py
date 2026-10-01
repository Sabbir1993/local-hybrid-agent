import json
import sqlite3
import sys
import time
from pathlib import PurePosixPath, PureWindowsPath
from typing import Optional

from ..sqlite_util import transaction
from .common import _forget_session_memory, _get_projects_db, _projects_db


def _proj_row(r) -> dict:
    pats = []
    try:
        if "allow_patterns" in r.keys() and r["allow_patterns"]:
            pats = json.loads(r["allow_patterns"])
    except Exception:
        pats = []
    dev_id = r["device_id"] if "device_id" in r.keys() else "default"
    dev_name = r["device_name"] if "device_name" in r.keys() else "Default Device"
    return {
        "id": r["id"],
        "name": r["name"],
        "created_at": r["created_at"],
        "workspace_dir": r["workspace_dir"],
        "allow_patterns": pats,
        "device_id": dev_id or "default",
        "device_name": dev_name or "Default Device",
    }


def db_list_projects(owner_user_id: int, device_id: Optional[str] = None, device_name: Optional[str] = None) -> list:
    """Strict per-user, per-device isolation."""
    if device_id:
        rows = _projects_db.execute(
            """SELECT * FROM projects
               WHERE user_id = ?
               AND device_id IS NOT NULL
               AND device_id = ?
               ORDER BY name""",
            (owner_user_id, device_id)
        ).fetchall()
    else:
        rows = _projects_db.execute(
            "SELECT * FROM projects WHERE user_id = ? ORDER BY name", (owner_user_id,)
        ).fetchall()
    return [_proj_row(r) for r in rows]


def db_project_owner(pid: int) -> Optional[int]:
    pdb = _get_projects_db()
    row = pdb.execute("SELECT user_id FROM projects WHERE id = ?", (pid,)).fetchone()
    return row["user_id"] if row else None


def db_owned_project_id(name_or_id, owner_user_id: int) -> Optional[int]:
    """id of the project referenced by id or name and owned by owner_user_id."""
    if name_or_id is None or owner_user_id is None:
        return None
    ref = str(name_or_id).strip()
    pdb = _get_projects_db()
    if ref.isdigit():
        row = pdb.execute("SELECT id FROM projects WHERE id = ? AND user_id = ?",
                          (int(ref), owner_user_id)).fetchone()
        if row:
            return row["id"]
    row = pdb.execute("SELECT id FROM projects WHERE name = ? AND user_id = ?",
                      (ref, owner_user_id)).fetchone()
    return row["id"] if row else None


def db_session_owner(sid: int) -> Optional[int]:
    pdb = _get_projects_db()
    row = pdb.execute("SELECT user_id FROM sessions WHERE id = ?", (sid,)).fetchone()
    return row["user_id"] if row else None


def db_get_project_allow_patterns(name_or_id) -> list:
    """Return list of allowed command patterns for a specific project."""
    if not name_or_id:
        return []
    try:
        if isinstance(name_or_id, int) or (isinstance(name_or_id, str) and name_or_id.isdigit()):
            row = _projects_db.execute("SELECT allow_patterns FROM projects WHERE id = ?", (int(name_or_id),)).fetchone()
        else:
            row = _projects_db.execute("SELECT allow_patterns FROM projects WHERE name = ?", (str(name_or_id),)).fetchone()
        if row and row["allow_patterns"]:
            return json.loads(row["allow_patterns"]) or []
    except Exception:
        pass
    return []


def db_add_project_allow_pattern(name_or_id, pattern: str) -> list:
    """Add a command pattern to a project's allowed patterns list."""
    pattern = pattern.strip()
    if not pattern or not name_or_id:
        return []
    pats = db_get_project_allow_patterns(name_or_id)
    if pattern not in pats:
        pats.append(pattern)
        val = json.dumps(pats)
        if isinstance(name_or_id, int) or (isinstance(name_or_id, str) and name_or_id.isdigit()):
            _projects_db.execute("UPDATE projects SET allow_patterns = ? WHERE id = ?", (val, int(name_or_id)))
        else:
            _projects_db.execute("UPDATE projects SET allow_patterns = ? WHERE name = ?", (val, str(name_or_id)))
        _projects_db.commit()
    return pats


def _client_workspace_path(workspace_dir: Optional[str]) -> str:
    """Validate a project folder path that lives on the USER's machine."""
    raw = (workspace_dir or "").strip()
    if not raw:
        raise ValueError("pick a folder on your machine for this project")
    if not (PureWindowsPath(raw).is_absolute() or PurePosixPath(raw).is_absolute()):
        raise ValueError("workspace_dir must be an absolute path on your machine")
    if ".." in PureWindowsPath(raw).parts or ".." in PurePosixPath(raw).parts:
        raise ValueError("workspace_dir must not contain '..'")
    return raw


def db_create_project(name: str, workspace_dir: str = None,
                       owner_user_id: int = None, device_id: str = "default", device_name: str = "Default Device") -> dict:
    if owner_user_id is None:
        raise ValueError("owner_user_id required")
    name = (name or "").strip()
    if not name:
        raise ValueError("project name required")
    if any(c in name for c in '<>:"/\\|?*'):
        raise ValueError("project name contains invalid path characters")
    ws = _client_workspace_path(workspace_dir)
    now = time.time()
    try:
        cur = _projects_db.execute(
            "INSERT INTO projects (name, created_at, workspace_dir, user_id, device_id, device_name) VALUES (?, ?, ?, ?, ?, ?)",
            (name, now, ws, owner_user_id, device_id or "default", device_name or "Default Device"))
        _projects_db.commit()
    except sqlite3.IntegrityError:
        raise ValueError(f"project '{name}' already exists on this device")
    return {"id": cur.lastrowid, "name": name, "created_at": now, "workspace_dir": ws,
            "device_id": device_id or "default", "device_name": device_name or "Default Device"}


def db_update_project_workspace(pid: int, workspace_dir: str, owner_user_id: int,
                                device_id: Optional[str] = None, device_name: Optional[str] = None) -> dict:
    if db_project_owner(pid) != owner_user_id:
        raise PermissionError("not your project")
    ws = _client_workspace_path(workspace_dir)

    updates = ["workspace_dir = ?"]
    params = [ws]
    if device_id:
        updates.append("device_id = ?")
        params.append(device_id)
    if device_name:
        updates.append("device_name = ?")
        params.append(device_name)
    params.extend([pid, owner_user_id])
    
    _projects_db.execute(
        f"UPDATE projects SET {', '.join(updates)} WHERE id = ? AND user_id = ?",
        params
    )
    _projects_db.commit()
    row = _projects_db.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
    return _proj_row(row) if row else {}


def db_rename_device(owner_user_id: int, new_name: str, device_id: Optional[str] = None, old_name: Optional[str] = None) -> int:
    """Update device_name for all projects matching device_id or old_name."""
    new_name = (new_name or "").strip()
    if not new_name:
        return 0
    if device_id:
        cur = _projects_db.execute(
            "UPDATE projects SET device_name = ? WHERE user_id = ? AND (device_id = ? OR device_id IS NULL OR device_id = 'default')",
            (new_name, owner_user_id, device_id)
        )
    elif old_name:
        cur = _projects_db.execute(
            "UPDATE projects SET device_name = ? WHERE user_id = ? AND device_name = ?",
            (new_name, owner_user_id, old_name)
        )
    else:
        cur = _projects_db.execute(
            "UPDATE projects SET device_name = ? WHERE user_id = ?",
            (new_name, owner_user_id)
        )
    _projects_db.commit()
    return cur.rowcount


def db_list_user_devices(owner_user_id: int) -> list:
    """Return distinct devices registered across this user's projects."""
    rows = _projects_db.execute(
        """SELECT DISTINCT device_id, device_name
           FROM projects
           WHERE user_id = ? AND device_id IS NOT NULL AND device_id != ''
           ORDER BY device_name""",
        (owner_user_id,)
    ).fetchall()
    return [{"device_id": r["device_id"], "device_name": r["device_name"]} for r in rows]


def db_delete_project(pid: int, owner_user_id: int) -> None:
    pdb = _get_projects_db()
    if db_project_owner(pid) != owner_user_id:
        raise PermissionError("not your project")
    sids = [r[0] for r in pdb.execute("SELECT id FROM sessions WHERE project_id = ?", (pid,))]
    with transaction(pdb) as c:
        c.execute("DELETE FROM plan_items WHERE session_id IN (SELECT id FROM sessions WHERE project_id = ?)",
                  (pid,))
        c.execute("DELETE FROM session_working_memory WHERE session_id IN "
                  "(SELECT id FROM sessions WHERE project_id = ?)", (pid,))
        c.execute("DELETE FROM messages WHERE session_id IN (SELECT id FROM sessions WHERE project_id = ?)",
                  (pid,))
        c.execute("DELETE FROM sessions WHERE project_id = ?", (pid,))
        c.execute("DELETE FROM projects WHERE id = ?", (pid,))
    _forget_session_memory(sids)


def db_session_docs(limit: int = 60) -> list:
    """Recent sessions as (path, text, version) docs for memory indexing."""
    out = []
    try:
        rows = _projects_db.execute(
            "SELECT s.id, s.title, s.created_at, "
            "(SELECT m.content FROM messages m WHERE m.session_id = s.id AND m.role = 'user' "
            " ORDER BY m.id LIMIT 1) AS first_user "
            "FROM sessions s ORDER BY s.created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        for r in rows:
            first = str(r["first_user"] or "").strip()[:500]
            out.append((f"session:{r['id']}", f"{r['title']}\n{first}", float(r["created_at"])))
    except Exception as e:
        print(f"[db] session docs failed: {e}", file=sys.stderr)
    return out
