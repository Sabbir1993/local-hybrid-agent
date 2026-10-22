"""Run journal (usage.db): one row per chat / agent / sub-agent / job run.

Holds state and stage CODES only -- never prompts, answers, tool output or SSE frames (those can
carry card numbers; routes/agent/run_manager.py keeps frames in memory on purpose). `ref` and
`reason` are short fixed-vocabulary strings and are PAN-masked before they are stored.

Writes are best-effort: a journal failure must never break a request.
"""

import sys
import time
from typing import Optional

from .db import _usage_db

KINDS = ("chat", "agent", "subagent", "deck", "kb_ingest", "media")
OPEN_STATES = ("running",)
END_STATES = ("completed", "stopped", "failed", "cancelled", "interrupted")
RETENTION_DAYS = 30


def _init() -> None:
    c = _usage_db
    c.execute("""CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY,
        user_id INTEGER,
        kind TEXT NOT NULL,
        parent_id TEXT,
        session_id TEXT,
        state TEXT NOT NULL DEFAULT 'running',
        stage TEXT,
        step INTEGER DEFAULT 0,
        reason TEXT,
        resumable INTEGER DEFAULT 0,
        ref TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_runs_user_state ON runs(user_id, state)")
    c.commit()


try:
    _init()
except Exception as e:
    print(f"[runs] init failed: {e}", file=sys.stderr)


def _clean(text: Optional[str], limit: int = 120) -> Optional[str]:
    if not text:
        return None
    try:
        from core.pan import mask_pans
        text = mask_pans(str(text))[0]
    except Exception:
        text = str(text)
    return text[:limit]


def _exec(sql: str, params: tuple = ()) -> int:
    try:
        cur = _usage_db.execute(sql, params)
        _usage_db.commit()
        return getattr(cur, "rowcount", 0) or 0
    except Exception as e:
        print(f"[runs] write failed: {e}", file=sys.stderr)
        return 0


def begin(run_id: str, kind: str, user_id: Optional[int] = None, *, session_id: Optional[str] = None,
          parent_id: Optional[str] = None, stage: Optional[str] = None, ref: Optional[str] = None,
          resumable: bool = False) -> None:
    now = time.time()
    _exec("INSERT OR REPLACE INTO runs (run_id, user_id, kind, parent_id, session_id, state, stage, step, "
          "reason, resumable, ref, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'running', ?, 0, NULL, ?, ?, ?, ?)",
          (run_id, user_id, kind, parent_id, session_id and str(session_id)[:64], _clean(stage, 40),
           1 if resumable else 0, _clean(ref), now, now))


def stage(run_id: str, code: str, step: Optional[int] = None) -> None:
    """Record the run's current stage (and optionally step) while it is still running."""
    if step is None:
        _exec("UPDATE runs SET stage = ?, updated_at = ? WHERE run_id = ? AND state = 'running'",
              (_clean(code, 40), time.time(), run_id))
    else:
        _exec("UPDATE runs SET stage = ?, step = ?, updated_at = ? WHERE run_id = ? AND state = 'running'",
              (_clean(code, 40), int(step), time.time(), run_id))


def end(run_id: str, state: str, reason: Optional[str] = None, *, step: Optional[int] = None,
        resumable: Optional[bool] = None) -> None:
    """Close a run. Only a still-running row is changed, so a late call never rewrites a final state."""
    if state not in END_STATES:
        state = "failed"
    _exec("UPDATE runs SET state = ?, reason = ?, step = COALESCE(?, step), "
          "resumable = COALESCE(?, resumable), updated_at = ? WHERE run_id = ? AND state = 'running'",
          (state, _clean(reason, 60), step, None if resumable is None else (1 if resumable else 0),
           time.time(), run_id))


def _rows(sql: str, params: tuple = ()) -> list:
    """usage.db has no row_factory, so build the dicts from the cursor description."""
    try:
        cur = _usage_db.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as e:
        print(f"[runs] read failed: {e}", file=sys.stderr)
        return []


def get(run_id: str) -> Optional[dict]:
    rows = _rows("SELECT * FROM runs WHERE run_id = ?", (run_id,))
    return rows[0] if rows else None


def open_runs(user_id: Optional[int] = None, kind: Optional[str] = None) -> list:
    sql, params = "SELECT * FROM runs WHERE state = 'running'", []
    if user_id is not None:
        sql += " AND user_id = ?"
        params.append(user_id)
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    return _rows(sql + " ORDER BY created_at", tuple(params))


def recent(user_id: int, kind: Optional[str] = None, state: Optional[str] = None, limit: int = 50) -> list:
    """This user's runs, running ones first then newest - what the Taskboard lists."""
    sql, params = "SELECT * FROM runs WHERE user_id = ?", [user_id]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    if state:
        sql += " AND state = ?"
        params.append(state)
    sql += " ORDER BY (state = 'running') DESC, updated_at DESC LIMIT ?"
    params.append(max(1, min(int(limit), 200)))
    return _rows(sql, tuple(params))


def interrupted(user_id: Optional[int] = None, kind: Optional[str] = None) -> list:
    sql, params = "SELECT * FROM runs WHERE state = 'interrupted'", []
    if user_id is not None:
        sql += " AND user_id = ?"
        params.append(user_id)
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    return _rows(sql + " ORDER BY updated_at DESC", tuple(params))


def reconcile_interrupted(only: Optional[list] = None) -> int:
    """At startup nothing is running yet: every row still 'running' belongs to a process that died.
    Mark them interrupted and close the matching route_runs rows left with no outcome.
    `only` limits it to those run ids (tests share the real usage.db, so they must not touch live rows)."""
    scope, params = "", ()
    if only is not None:
        if not only:
            return 0
        scope, params = f" AND run_id IN ({','.join('?' * len(only))})", tuple(only)
    n = _exec("UPDATE runs SET state = 'interrupted', reason = COALESCE(reason, 'server_restart'), updated_at = ? "
              "WHERE state = 'running'" + scope, (time.time(),) + params)
    try:
        _usage_db.execute("UPDATE route_runs SET outcome = 'interrupted' WHERE outcome IS NULL" + scope, params)
        _usage_db.commit()
    except Exception as e:
        print(f"[runs] route_runs reconcile failed: {e}", file=sys.stderr)
    return n


def purge(days: int = RETENTION_DAYS) -> None:
    _exec("DELETE FROM runs WHERE state != 'running' AND updated_at < ?", (time.time() - days * 86400,))
