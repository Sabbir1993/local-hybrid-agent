"""Routing telemetry (usage.db): what the agent loop decided per step, how each
run ended, user thumbs, and the tuner's pending suggestions.

No prompt or answer text is stored -- only the query category from
core.router_policy.classify_query, lane/reason codes, tool names and ok flags --
so card data, merchant references or code never land in analytics tables.
Rows older than RETENTION_DAYS are purged on startup and after each tuner run.
"""

import json
import sys
import time
from typing import Optional

from .db import _usage_db

RETENTION_DAYS = 90


def _init() -> None:
    c = _usage_db
    c.execute("""CREATE TABLE IF NOT EXISTS route_runs (
        run_id TEXT PRIMARY KEY,
        ts REAL NOT NULL,
        user_id INTEGER,
        mode TEXT,
        category TEXT,
        steps INTEGER DEFAULT 0,
        outcome TEXT,
        rating INTEGER
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS route_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        run_id TEXT NOT NULL,
        step INTEGER,
        category TEXT,
        lane TEXT,
        reason TEXT,
        router_tool TEXT,
        router_conf REAL,
        escalated INTEGER DEFAULT 0,
        escalate_reason TEXT,
        tool_name TEXT,
        tool_ok INTEGER,
        duration_s REAL
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS router_suggestions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created REAL NOT NULL,
        key TEXT NOT NULL,
        current TEXT,
        proposed TEXT,
        evidence TEXT,
        status TEXT DEFAULT 'pending',
        decided_by TEXT,
        decided_at REAL
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_route_runs_ts ON route_runs(ts)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_route_events_run ON route_events(run_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_route_events_ts ON route_events(ts)")
    c.commit()


try:
    _init()
except Exception as e:
    print(f"[route_log] init failed: {e}", file=sys.stderr)


def _exec(sql: str, params: tuple = ()) -> None:
    try:
        _usage_db.execute(sql, params)
        _usage_db.commit()
    except Exception as e:
        print(f"[route_log] write failed: {e}", file=sys.stderr)


# ---------------------------------------------------------------- writes

def run_start(run_id: str, user_id: Optional[int], mode: str, category: str) -> None:
    _exec("INSERT OR REPLACE INTO route_runs (run_id, ts, user_id, mode, category) VALUES (?, ?, ?, ?, ?)",
          (run_id, time.time(), user_id, mode, category))


def run_end(run_id: str, steps: int, outcome: str) -> None:
    _exec("UPDATE route_runs SET steps = ?, outcome = ? WHERE run_id = ?", (steps, outcome, run_id))


def event(run_id: str, step: int, category: str, lane: str, reason: str, *,
          router_tool: Optional[str] = None, router_conf: Optional[float] = None,
          escalated: bool = False, escalate_reason: str = "",
          tool_name: Optional[str] = None, tool_ok: Optional[bool] = None,
          duration_s: Optional[float] = None) -> None:
    _exec("INSERT INTO route_events (ts, run_id, step, category, lane, reason, router_tool, router_conf, "
          "escalated, escalate_reason, tool_name, tool_ok, duration_s) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
          (time.time(), run_id, step, category, lane, reason, router_tool, router_conf,
           1 if escalated else 0, escalate_reason or None, tool_name,
           None if tool_ok is None else (1 if tool_ok else 0), duration_s))


def rate(run_id: str, user_id: int, rating: int) -> bool:
    """Thumbs on a run -- only the user who ran it may rate it."""
    try:
        cur = _usage_db.execute("UPDATE route_runs SET rating = ? WHERE run_id = ? AND user_id = ?",
                                (rating, run_id, user_id))
        _usage_db.commit()
        return cur.rowcount > 0
    except Exception as e:
        print(f"[route_log] rate failed: {e}", file=sys.stderr)
        return False


def purge(days: int = RETENTION_DAYS) -> None:
    cutoff = time.time() - days * 86400
    _exec("DELETE FROM route_events WHERE ts < ?", (cutoff,))
    _exec("DELETE FROM route_runs WHERE ts < ?", (cutoff,))


# ---------------------------------------------------------------- reads

def stats(days: int = 30) -> dict:
    """Per-category routing stats for the Settings -> Router table."""
    since = time.time() - days * 86400
    out = {}
    q = _usage_db.execute
    for cat, runs, avg_steps, up, down in q(
            "SELECT category, COUNT(*), AVG(steps), SUM(rating = 1), SUM(rating = -1) "
            "FROM route_runs WHERE ts >= ? GROUP BY category", (since,)):
        out[cat or "other"] = {"runs": runs, "avg_steps": round(avg_steps or 0, 2),
                               "thumbs_up": up or 0, "thumbs_down": down or 0}
    for cat, ex_steps, esc in q(
            "SELECT e.category, COUNT(*), SUM(e.escalated) FROM route_events e "
            "WHERE e.ts >= ? AND e.lane = 'executor' AND e.step = 0 AND e.tool_name IS NULL "
            "GROUP BY e.category", (since,)):
        d = out.setdefault(cat or "other", {"runs": 0, "avg_steps": 0, "thumbs_up": 0, "thumbs_down": 0})
        d["executor_first_steps"] = ex_steps
        d["escalation_rate"] = round((esc or 0) / ex_steps, 3) if ex_steps else None
    for cat, hits, passes in q(
            "SELECT category, SUM(reason = 'router_hit'), SUM(reason = 'router_pass') FROM route_events "
            "WHERE ts >= ? AND step = 0 AND tool_name IS NULL GROUP BY category", (since,)):
        d = out.setdefault(cat or "other", {"runs": 0, "avg_steps": 0, "thumbs_up": 0, "thumbs_down": 0})
        tot = (hits or 0) + (passes or 0)
        d["router_hit_rate"] = round((hits or 0) / tot, 3) if tot else None
    lanes = {}
    for lane, n, ok_n, tools in q(
            "SELECT lane, COUNT(*), SUM(tool_ok = 1), SUM(tool_name IS NOT NULL) FROM route_events "
            "WHERE ts >= ? GROUP BY lane", (since,)):
        lanes[lane or "?"] = {"events": n, "tool_calls": tools or 0,
                              "tool_ok_rate": round((ok_n or 0) / tools, 3) if tools else None}
    outcomes = {o or "unknown": n for o, n in q(
        "SELECT outcome, COUNT(*) FROM route_runs WHERE ts >= ? GROUP BY outcome", (since,))}
    return {"days": days, "categories": out, "lanes": lanes, "outcomes": outcomes}


# ---------------------------------------------------------------- suggestions

def add_suggestion(key: str, current, proposed, evidence: dict) -> Optional[int]:
    """Insert unless an identical pending suggestion already exists."""
    cur_s, prop_s = json.dumps(current), json.dumps(proposed)
    row = _usage_db.execute("SELECT id FROM router_suggestions WHERE status = 'pending' AND key = ? AND proposed = ?",
                            (key, prop_s)).fetchone()
    if row:
        _exec("UPDATE router_suggestions SET evidence = ?, current = ?, created = ? WHERE id = ?",
              (json.dumps(evidence), cur_s, time.time(), row[0]))
        return row[0]
    try:
        c = _usage_db.execute("INSERT INTO router_suggestions (created, key, current, proposed, evidence) "
                              "VALUES (?, ?, ?, ?, ?)", (time.time(), key, cur_s, prop_s, json.dumps(evidence)))
        _usage_db.commit()
        return c.lastrowid
    except Exception as e:
        print(f"[route_log] add_suggestion failed: {e}", file=sys.stderr)
        return None


def _sugg_row(r) -> dict:
    return {"id": r[0], "created": r[1], "key": r[2], "current": json.loads(r[3]) if r[3] else None,
            "proposed": json.loads(r[4]) if r[4] else None, "evidence": json.loads(r[5]) if r[5] else {},
            "status": r[6], "decided_by": r[7], "decided_at": r[8]}


def list_suggestions(status: Optional[str] = None, limit: int = 50) -> list:
    sql = "SELECT id, created, key, current, proposed, evidence, status, decided_by, decided_at FROM router_suggestions"
    params: tuple = ()
    if status:
        sql += " WHERE status = ?"
        params = (status,)
    sql += " ORDER BY created DESC LIMIT ?"
    return [_sugg_row(r) for r in _usage_db.execute(sql, params + (limit,))]


def get_suggestion(sid: int) -> Optional[dict]:
    r = _usage_db.execute("SELECT id, created, key, current, proposed, evidence, status, decided_by, decided_at "
                          "FROM router_suggestions WHERE id = ?", (sid,)).fetchone()
    return _sugg_row(r) if r else None


def decide_suggestion(sid: int, status: str, decided_by: str) -> None:
    _exec("UPDATE router_suggestions SET status = ?, decided_by = ?, decided_at = ? WHERE id = ?",
          (status, decided_by, time.time(), sid))
