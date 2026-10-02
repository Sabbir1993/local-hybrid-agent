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
    # additive migrations on an existing usage.db: step outcome code (core/step_outcome.py)
    # and the model that produced the step. Codes/ids only, never text.
    have_runs = {r[1] for r in c.execute("PRAGMA table_info(route_runs)")}
    if "detail" not in have_runs:
        c.execute("ALTER TABLE route_runs ADD COLUMN detail TEXT")     # e.g. identical_result:run_python:3
    have = {r[1] for r in c.execute("PRAGMA table_info(route_events)")}
    for col, decl in (("outcome", "TEXT"), ("model", "TEXT"), ("clf", "TEXT"),
                      ("tool_err", "TEXT")):
        if col not in have:
            c.execute(f"ALTER TABLE route_events ADD COLUMN {col} {decl}")
    # changes the tuner applied on its own, watched against a before/after quality window
    c.execute("""CREATE TABLE IF NOT EXISTS router_applied (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        suggestion_id INTEGER,
        key TEXT NOT NULL,
        old TEXT,
        new TEXT,
        category TEXT,
        applied_at REAL NOT NULL,
        baseline TEXT,
        status TEXT DEFAULT 'watching',
        result TEXT,
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


# Failure taxonomy for tool results: a FIXED code vocabulary, ordered most
# specific first. Codes, never text - error strings carry file paths and
# content snippets, and usage.db is retained data. Without this, "edit_file
# fails 6% of the time" is unactionable: the number never says which of
# no-match / ambiguous / drift / verify-failed the model actually hit.
_TOOL_ERR_RULES = (
    ("no model is available", None, "no_model"),
    ("unknown role", None, "unknown_role"),
    ("requires a non-empty task", None, "invalid_args"),
    ("hunk", "drifted", "diff_drift"),
    ("hunk", "match", "ambiguous"),
    ("hunks overlap", None, "diff_malformed"),
    ("no hunks", None, "diff_malformed"),
    ("diff is empty", None, "diff_malformed"),
    ("not part of a unified diff", None, "diff_malformed"),
    ("inserts at line", None, "diff_malformed"),
    ("either diff or", None, "invalid_args"),
    ("not found in the file", None, "no_match"),
    ("appears", "times", "ambiguous"),
    ("matches", "places", "ambiguous"),
    ("close to the text at", None, "ambiguous"),
    ("is empty", None, "invalid_args"),
    ("are identical", None, "invalid_args"),
    ("sandbox violation", None, "sandbox"),
    ("traverse", None, "sandbox"),
    ("before editing", None, "not_read"),
    ("before inserting", None, "not_read"),
    ("does not exist", None, "not_found"),
    ("File not found", None, "not_found"),
    ("already exists", None, "exists"),
    ("verify: FAILED", None, "verify_failed"),
    ("must be an integer", None, "invalid_args"),
)


def classify_tool_result(result) -> Optional[str]:
    """Fixed failure code for a tool result string, or None when it succeeded.

    Ordered substring rules, most specific first. Unknown failures collapse to
    "error" rather than inventing a code per message: the vocabulary has to stay
    small enough to group 1000s of rows, and the raw text is never stored.
    """
    if not isinstance(result, str):
        return None
    if result.startswith("verify: OK"):
        return None
    if not result:
        return "error"        # a successful tool never returns nothing
    if not (result.startswith("error:") or result.startswith("File not found")):
        return None
    for rule in _TOOL_ERR_RULES:
        first, second, code = rule
        if first in result and (second is None or second in result):
            return code
    return "error"


# ---------------------------------------------------------------- writes

def run_start(run_id: str, user_id: Optional[int], mode: str, category: str) -> None:
    _exec("INSERT OR REPLACE INTO route_runs (run_id, ts, user_id, mode, category) VALUES (?, ?, ?, ?, ?)",
          (run_id, time.time(), user_id, mode, category))


def run_end(run_id: str, steps: int, outcome: str, detail: Optional[str] = None) -> None:
    _exec("UPDATE route_runs SET steps = ?, outcome = ?, detail = ? WHERE run_id = ?",
          (steps, outcome, detail, run_id))


def event(run_id: str, step: int, category: str, lane: str, reason: str, *,
          router_tool: Optional[str] = None, router_conf: Optional[float] = None,
          escalated: bool = False, escalate_reason: str = "",
          tool_name: Optional[str] = None, tool_ok: Optional[bool] = None,
          duration_s: Optional[float] = None,
          outcome: Optional[str] = None, model: Optional[str] = None,
          clf: Optional[str] = None, tool_err: Optional[str] = None) -> None:
    _exec("INSERT INTO route_events (ts, run_id, step, category, lane, reason, router_tool, router_conf, "
          "escalated, escalate_reason, tool_name, tool_ok, duration_s, outcome, model, clf, tool_err) "
          "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
          (time.time(), run_id, step, category, lane, reason, router_tool, router_conf,
           1 if escalated else 0, escalate_reason or None, tool_name,
           None if tool_ok is None else (1 if tool_ok else 0), duration_s, outcome, model, clf, tool_err))


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


def recent_turns(limit: int = 300) -> list:
    """Recent model turns, oldest first, as (lane, category, outcome, step, escalated)
    for core.lane_health. Router-lane rows and tool rows are not model turns.

    Rows written before outcome codes existed have none; for those a first step of a
    run that ended after one step as 'synthesized' or 'error' is read as no_tool_call
    (that is how a model that only narrated its step ended)."""
    since = time.time() - 7 * 86400
    rows = _usage_db.execute(
        "SELECT e.lane, e.category, "
        "COALESCE(e.outcome, CASE WHEN e.step = 0 AND r.steps = 1 AND r.outcome IN ('synthesized', 'error') "
        "THEN 'no_tool_call' END), e.step, e.escalated "
        "FROM route_events e LEFT JOIN route_runs r ON r.run_id = e.run_id "
        "WHERE e.ts >= ? AND e.tool_name IS NULL AND e.lane IN ('executor', 'main') "
        "AND e.reason NOT IN ('router_hit', 'router_pass') ORDER BY e.id DESC LIMIT ?",
        (since, limit)).fetchall()
    return rows[::-1]


# ---------------------------------------------------------------- applied changes

BAD_OUTCOMES = ("error", "loop", "loop_near_repeat", "no_progress", "max_steps", "timeout")


def run_quality(category: Optional[str] = None, *, since: float = 0.0, until: Optional[float] = None,
                limit: Optional[int] = None, newest_first: bool = False) -> dict:
    """{"runs": n, "bad": b}: finished runs (optionally one category, in a time window) and how
    many ended badly - an error, a loop, a step/time limit - or got a thumbs down.
    `limit` keeps the first (or, with newest_first, the latest) N runs of the window."""
    sql = "SELECT outcome, rating FROM route_runs WHERE outcome IS NOT NULL AND outcome != 'cancelled' AND ts >= ?"
    params: list = [since]
    if until is not None:
        sql += " AND ts < ?"
        params.append(until)
    if category:
        sql += " AND category = ?"
        params.append(category)
    sql += " ORDER BY ts " + ("DESC" if newest_first else "ASC")
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    rows = _usage_db.execute(sql, tuple(params)).fetchall()
    bad = sum(1 for o, r in rows if o in BAD_OUTCOMES or r == -1)
    return {"runs": len(rows), "bad": bad}


def add_applied(suggestion_id: Optional[int], key: str, old, new, category: Optional[str],
                baseline: dict) -> Optional[int]:
    try:
        c = _usage_db.execute(
            "INSERT INTO router_applied (suggestion_id, key, old, new, category, applied_at, baseline) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (suggestion_id, key, json.dumps(old), json.dumps(new), category, time.time(), json.dumps(baseline)))
        _usage_db.commit()
        return c.lastrowid
    except Exception as e:
        print(f"[route_log] add_applied failed: {e}", file=sys.stderr)
        return None


def _applied_row(r) -> dict:
    return {"id": r[0], "suggestion_id": r[1], "key": r[2], "old": json.loads(r[3]) if r[3] else None,
            "new": json.loads(r[4]) if r[4] else None, "category": r[5], "applied_at": r[6],
            "baseline": json.loads(r[7]) if r[7] else {}, "status": r[8],
            "result": json.loads(r[9]) if r[9] else None, "decided_at": r[10]}


_APPLIED_COLS = "id, suggestion_id, key, old, new, category, applied_at, baseline, status, result, decided_at"


def list_applied(status: Optional[str] = None, limit: int = 50) -> list:
    sql = f"SELECT {_APPLIED_COLS} FROM router_applied"
    params: tuple = ()
    if status:
        sql += " WHERE status = ?"
        params = (status,)
    sql += " ORDER BY applied_at DESC LIMIT ?"
    return [_applied_row(r) for r in _usage_db.execute(sql, params + (limit,))]


def get_applied(aid: int) -> Optional[dict]:
    r = _usage_db.execute(f"SELECT {_APPLIED_COLS} FROM router_applied WHERE id = ?", (aid,)).fetchone()
    return _applied_row(r) if r else None


def set_applied_status(aid: int, status: str, result: Optional[dict] = None) -> None:
    _exec("UPDATE router_applied SET status = ?, result = ?, decided_at = ? WHERE id = ?",
          (status, json.dumps(result) if result is not None else None, time.time(), aid))


def recently_rolled_back(key: str, proposed, days: int = 7) -> bool:
    """Was this exact change rolled back lately? (the tuner must not re-propose it at once)"""
    row = _usage_db.execute(
        "SELECT 1 FROM router_applied WHERE status = 'rolled_back' AND key = ? AND new = ? AND decided_at >= ? LIMIT 1",
        (key, json.dumps(proposed), time.time() - days * 86400)).fetchone()
    return bool(row)


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
