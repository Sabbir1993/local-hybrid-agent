import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

from .config import USAGE_DB_FILE, PROJECTS_DB_FILE

# ---------------- usage tracking (sqlite) ----------------
def _init_usage_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(USAGE_DB_FILE), check_same_thread=False)
    conn.execute("""CREATE TABLE IF NOT EXISTS requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        endpoint TEXT NOT NULL,
        model TEXT,
        prompt_tokens INTEGER,
        completion_tokens INTEGER,
        total_tokens INTEGER,
        tps REAL,
        duration_s REAL,
        prompt_tps REAL,
        stream INTEGER,
        status INTEGER
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests(ts)")
    conn.commit()
    return conn


_usage_db = _init_usage_db()


def db_record_request(endpoint: str, model: Optional[str], prompt_tokens: Optional[int],
                      completion_tokens: Optional[int], tps: Optional[float],
                      duration_s: float, prompt_tps: Optional[float],
                      stream: bool, status: int) -> None:
    """Persist one completed request."""
    try:
        _usage_db.execute(
            "INSERT INTO requests (ts, endpoint, model, prompt_tokens, completion_tokens, total_tokens, tps, duration_s, prompt_tps, stream, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), endpoint, model, prompt_tokens, completion_tokens,
             (prompt_tokens or 0) + (completion_tokens or 0), tps, duration_s,
             prompt_tps, 1 if stream else 0, status),
        )
        _usage_db.commit()
    except Exception as e:
        print(f"[server_manager] usage db insert failed: {e}", file=sys.stderr)


def db_report(days: int = 30, model: Optional[str] = None) -> dict:
    """Aggregate token usage report."""
    since = time.time() - days * 86400
    q = "SELECT COUNT(*), COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0), COALESCE(SUM(total_tokens),0), COALESCE(AVG(tps),0), COALESCE(AVG(duration_s),0) FROM requests WHERE ts >= ?"
    args: list = [since]
    if model:
        q += " AND model = ?"
        args.append(model)
    row = _usage_db.execute(q, args).fetchone()
    by_model = _usage_db.execute(
        "SELECT model, COUNT(*), SUM(prompt_tokens), SUM(completion_tokens), SUM(total_tokens) FROM requests WHERE ts >= ? GROUP BY model ORDER BY SUM(total_tokens) DESC",
        (since,),
    ).fetchall()
    by_day = _usage_db.execute(
        "SELECT date(ts, 'unixepoch') AS d, COUNT(*), SUM(prompt_tokens), SUM(completion_tokens), SUM(total_tokens) FROM requests WHERE ts >= ? GROUP BY d ORDER BY d DESC",
        (since,),
    ).fetchall()
    return {
        "days": days,
        "requests": row[0],
        "prompt_tokens": row[1],
        "completion_tokens": row[2],
        "total_tokens": row[3],
        "avg_tps": round(row[4], 2) if row[4] else 0,
        "avg_duration_s": round(row[5], 2) if row[5] else 0,
        "by_model": [
            {"model": m or "unknown", "requests": c, "prompt_tokens": p, "completion_tokens": g, "total_tokens": t}
            for m, c, p, g, t in by_model
        ],
        "by_day": [
            {"day": d, "requests": c, "prompt_tokens": p, "completion_tokens": g, "total_tokens": t}
            for d, c, p, g, t in by_day
        ],
    }


# ---------------- projects & sessions (sqlite) ----------------
def _init_projects_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(PROJECTS_DB_FILE), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        created_at REAL NOT NULL,
        workspace_dir TEXT
    );
    CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
        title TEXT NOT NULL,
        created_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        meta TEXT,
        created_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
    """)
    # migration: older DBs lack the workspace_dir column or allow_patterns
    cols = [r[1] for r in conn.execute("PRAGMA table_info(projects)")]
    if "workspace_dir" not in cols:
        conn.execute("ALTER TABLE projects ADD COLUMN workspace_dir TEXT")
    if "allow_patterns" not in cols:
        conn.execute("ALTER TABLE projects ADD COLUMN allow_patterns TEXT DEFAULT '[]'")
    conn.commit()
    return conn


_projects_db = _init_projects_db()


def _proj_row(r) -> dict:
    import json as _json
    pats = []
    try:
        if "allow_patterns" in r.keys() and r["allow_patterns"]:
            pats = _json.loads(r["allow_patterns"])
    except Exception:
        pats = []
    return {"id": r["id"], "name": r["name"], "created_at": r["created_at"],
            "workspace_dir": r["workspace_dir"], "allow_patterns": pats}


def db_list_projects() -> list:
    return [_proj_row(r) for r in _projects_db.execute("SELECT * FROM projects ORDER BY name")]


def db_get_project_allow_patterns(name_or_id) -> list:
    """Return list of allowed command patterns for a specific project."""
    import json as _json
    if not name_or_id:
        return []
    try:
        if isinstance(name_or_id, int) or (isinstance(name_or_id, str) and name_or_id.isdigit()):
            row = _projects_db.execute("SELECT allow_patterns FROM projects WHERE id = ?", (int(name_or_id),)).fetchone()
        else:
            row = _projects_db.execute("SELECT allow_patterns FROM projects WHERE name = ?", (str(name_or_id),)).fetchone()
        if row and row["allow_patterns"]:
            return _json.loads(row["allow_patterns"]) or []
    except Exception:
        pass
    return []


def db_add_project_allow_pattern(name_or_id, pattern: str) -> list:
    """Add a command pattern to a project's allowed patterns list."""
    import json as _json
    pattern = pattern.strip()
    if not pattern or not name_or_id:
        return []
    pats = db_get_project_allow_patterns(name_or_id)
    if pattern not in pats:
        pats.append(pattern)
        val = _json.dumps(pats)
        if isinstance(name_or_id, int) or (isinstance(name_or_id, str) and name_or_id.isdigit()):
            _projects_db.execute("UPDATE projects SET allow_patterns = ? WHERE id = ?", (val, int(name_or_id)))
        else:
            _projects_db.execute("UPDATE projects SET allow_patterns = ? WHERE name = ?", (val, str(name_or_id)))
        _projects_db.commit()
    return pats


def db_create_project(name: str, workspace_dir: str = None, workspace_root: Path = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("project name required")
    if any(c in name for c in '<>:"/\\|?*'):
        raise ValueError("project name contains invalid path characters")
    ws = None
    if workspace_dir and workspace_dir.strip():
        p = Path(workspace_dir.strip()).expanduser()
        if not p.is_absolute():
            raise ValueError("workspace_dir must be an absolute path")
        p.mkdir(parents=True, exist_ok=True)
        ws = str(p.resolve())
    now = time.time()
    try:
        cur = _projects_db.execute(
            "INSERT INTO projects (name, created_at, workspace_dir) VALUES (?, ?, ?)",
            (name, now, ws))
        _projects_db.commit()
    except sqlite3.IntegrityError:
        raise ValueError(f"project '{name}' already exists")
    if not ws and workspace_root:
        d = workspace_root / name
        d.mkdir(parents=True, exist_ok=True)
    return {"id": cur.lastrowid, "name": name, "created_at": now, "workspace_dir": ws}


def db_delete_project(pid: int) -> None:
    _projects_db.execute(
        "DELETE FROM messages WHERE session_id IN (SELECT id FROM sessions WHERE project_id = ?)",
        (pid,)
    )
    _projects_db.execute("DELETE FROM sessions WHERE project_id = ?", (pid,))
    _projects_db.execute("DELETE FROM projects WHERE id = ?", (pid,))
    _projects_db.commit()


def db_list_sessions(pid: Optional[int]) -> list:
    if pid is None or pid == 0:
        rows = _projects_db.execute(
            "SELECT * FROM sessions WHERE project_id IS NULL ORDER BY id DESC"
        )
    else:
        rows = _projects_db.execute(
            "SELECT * FROM sessions WHERE project_id = ? ORDER BY id DESC", (pid,)
        )
    return [
        {"id": r["id"], "title": r["title"], "created_at": r["created_at"]}
        for r in rows
    ]


def db_create_session(pid: Optional[int], title: str = None) -> dict:
    now = time.time()
    title = (title or "New session").strip()[:80]
    actual_pid = None if (pid is None or pid == 0) else pid
    cur = _projects_db.execute(
        "INSERT INTO sessions (project_id, title, created_at) VALUES (?, ?, ?)",
        (actual_pid, title, now))
    _projects_db.commit()
    return {"id": cur.lastrowid, "title": title, "created_at": now}


def db_update_session_title(sid: int, title: str) -> None:
    title = (title or "New session").strip()[:80]
    _projects_db.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, sid))
    _projects_db.commit()


def db_delete_session(sid: int) -> None:
    _projects_db.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
    _projects_db.execute("DELETE FROM sessions WHERE id = ?", (sid,))
    _projects_db.commit()


def db_load_messages(sid: int) -> list:
    return [
        {"role": r["role"], "content": r["content"], "meta": json.loads(r["meta"]) if r["meta"] else None}
        for r in _projects_db.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY id", (sid,))
    ]


def db_append_message(sid: int, role: str, content: str, meta: dict = None) -> int:
    cur = _projects_db.execute(
        "INSERT INTO messages (session_id, role, content, meta, created_at) VALUES (?, ?, ?, ?, ?)",
        (sid, role, content, json.dumps(meta) if meta else None, time.time()))
    _projects_db.commit()
    return cur.lastrowid
