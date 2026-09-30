import sys
import time
from typing import Optional

from ..config import USAGE_DB_FILE
from ..sqlite_util import ThreadLocalDB


def _init_usage_db() -> ThreadLocalDB:
    conn = ThreadLocalDB(USAGE_DB_FILE)
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
        status INTEGER,
        prompt_cached_tokens INTEGER DEFAULT 0,
        completion_cached_tokens INTEGER DEFAULT 0,
        is_orchestrator INTEGER DEFAULT 0,
        source TEXT,
        provider TEXT,
        run_id TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests(ts)")
    # Schema migration for existing DB
    cols = [r[1] for r in conn.execute("PRAGMA table_info(requests)")]
    if "prompt_cached_tokens" not in cols:
        conn.execute("ALTER TABLE requests ADD COLUMN prompt_cached_tokens INTEGER DEFAULT 0")
    if "completion_cached_tokens" not in cols:
        conn.execute("ALTER TABLE requests ADD COLUMN completion_cached_tokens INTEGER DEFAULT 0")
    if "is_orchestrator" not in cols:
        conn.execute("ALTER TABLE requests ADD COLUMN is_orchestrator INTEGER DEFAULT 0")
    if "source" not in cols:
        conn.execute("ALTER TABLE requests ADD COLUMN source TEXT")
    if "provider" not in cols:
        conn.execute("ALTER TABLE requests ADD COLUMN provider TEXT")
    if "run_id" not in cols:                 # which agent run a request belongs to (per-run cost in the report)
        conn.execute("ALTER TABLE requests ADD COLUMN run_id TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_run ON requests(run_id)")
    # Backfill source for older records logged before cloud lanes existed
    conn.execute("UPDATE requests SET source = 'cloud' WHERE source IS NULL AND model LIKE 'cloud:%'")
    conn.execute("UPDATE requests SET source = 'local' WHERE source IS NULL")
    # Backfill is_orchestrator for older records
    conn.execute("UPDATE requests SET is_orchestrator = 1 WHERE is_orchestrator = 0 AND (LOWER(COALESCE(model,'')) LIKE '%orchestrator%' OR LOWER(endpoint) LIKE 'agent/%')")
    conn.commit()
    return conn


_usage_db = _init_usage_db()


def db_record_request(endpoint: str, model: Optional[str], prompt_tokens: Optional[int],
                      completion_tokens: Optional[int], tps: Optional[float],
                      duration_s: float, prompt_tps: Optional[float],
                      stream: bool, status: int,
                      prompt_cached_tokens: Optional[int] = 0,
                      completion_cached_tokens: Optional[int] = 0,
                      is_orchestrator: bool = False,
                      source: Optional[str] = None,
                      provider: Optional[str] = None,
                      run_id: Optional[str] = None) -> None:
    """Persist one completed request with input/output cache and orchestrator attribution."""
    try:
        m_lower = (model or "").lower()
        ep_lower = (endpoint or "").lower()
        if not is_orchestrator:
            if "orchestrator" in m_lower or "orchestrator" in ep_lower or ep_lower.startswith("agent/executor") or ep_lower.startswith("agent/needle") or ep_lower.startswith("agent/router"):
                is_orchestrator = True

        _usage_db.execute(
            "INSERT INTO requests (ts, endpoint, model, prompt_tokens, completion_tokens, total_tokens, tps, duration_s, prompt_tps, stream, status, prompt_cached_tokens, completion_cached_tokens, is_orchestrator, source, provider, run_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), endpoint, model, prompt_tokens or 0, completion_tokens or 0,
             (prompt_tokens or 0) + (completion_tokens or 0), tps, duration_s,
             prompt_tps, 1 if stream else 0, status,
             prompt_cached_tokens or 0, completion_cached_tokens or 0, 1 if is_orchestrator else 0,
             source or "local", provider, run_id),
        )
        _usage_db.commit()
    except Exception as e:
        print(f"[server_manager] usage db insert failed: {e}", file=sys.stderr)


def _day_bounds(start: Optional[str], end: Optional[str]):
    """(since, until) epoch seconds for local dates 'YYYY-MM-DD' (end day included); None when unusable."""
    def parse(v):
        try:
            return time.mktime(time.strptime(str(v).strip()[:10], "%Y-%m-%d"))
        except (ValueError, TypeError, OverflowError):
            return None
    a, b = parse(start) if start else None, parse(end) if end else None
    return a, (b + 86400 if b is not None else None)


def db_report(days: int = 30, model: Optional[str] = None,
              start: Optional[str] = None, end: Optional[str] = None) -> dict:
    """Aggregate token usage report including prompt/completion cache hits and orchestrator counts.

    `start` / `end` (local dates, inclusive) replace the rolling `days` window when given."""
    since = time.time() - days * 86400
    a, until = _day_bounds(start, end)
    if a is not None:
        since = a
    until_given = until is not None
    until = until if until_given else time.time() + 86400 * 366
    q = """
        SELECT
            COUNT(*),
            COALESCE(SUM(prompt_tokens), 0),
            COALESCE(SUM(completion_tokens), 0),
            COALESCE(SUM(total_tokens), 0),
            COALESCE(AVG(tps), 0),
            COALESCE(AVG(duration_s), 0),
            COALESCE(SUM(prompt_cached_tokens), 0),
            COALESCE(SUM(completion_cached_tokens), 0),
            COALESCE(SUM(is_orchestrator), 0),
            COALESCE(SUM(CASE WHEN is_orchestrator = 1 THEN total_tokens ELSE 0 END), 0)
        FROM requests
        WHERE ts >= ? AND ts < ?
    """
    params = [since, until]
    if model:
        q += " AND model = ?"
        params.append(model)

    row = _usage_db.execute(q, params).fetchone()
    total_reqs = row[0] or 0
    prompt_tokens = row[1] or 0
    completion_tokens = row[2] or 0
    total_tokens = row[3] or 0
    avg_tps = row[4] or 0.0
    avg_duration_s = row[5] or 0.0
    prompt_cached = row[6] or 0
    completion_cached = row[7] or 0
    total_cached = prompt_cached + completion_cached
    orchestrator_reqs = row[8] or 0
    orchestrator_total_tokens = row[9] or 0

    prompt_hit_rate = (prompt_cached / prompt_tokens) if prompt_tokens > 0 else 0.0
    completion_hit_rate = (completion_cached / completion_tokens) if completion_tokens > 0 else 0.0

    by_model_q = """
        SELECT
            model,
            COUNT(*),
            COALESCE(SUM(prompt_tokens), 0),
            COALESCE(SUM(prompt_cached_tokens), 0),
            COALESCE(SUM(completion_tokens), 0),
            COALESCE(SUM(completion_cached_tokens), 0),
            COALESCE(SUM(total_tokens), 0),
            COALESCE(AVG(tps), 0),
            COALESCE(SUM(is_orchestrator), 0),
            COALESCE(source, 'local'),
            provider
        FROM requests
        WHERE ts >= ? AND ts < ?
        GROUP BY model, COALESCE(source, 'local'), provider
        ORDER BY SUM(total_tokens) DESC
    """
    by_model_rows = _usage_db.execute(by_model_q, [since, until]).fetchall()

    by_day_q = """
        SELECT
            strftime('%Y-%m-%d', datetime(ts, 'unixepoch', 'localtime')) as day,
            COUNT(*),
            COALESCE(SUM(prompt_tokens), 0),
            COALESCE(SUM(prompt_cached_tokens), 0),
            COALESCE(SUM(completion_tokens), 0),
            COALESCE(SUM(completion_cached_tokens), 0),
            COALESCE(SUM(total_tokens), 0),
            COALESCE(SUM(is_orchestrator), 0)
        FROM requests
        WHERE ts >= ? AND ts < ?
    """
    by_day_params = [since, until]
    if model:
        by_day_q += " AND model = ?"
        by_day_params.append(model)
    by_day_q += " GROUP BY day ORDER BY day DESC"       # newest first, as the report table shows it
    by_day_rows = _usage_db.execute(by_day_q, by_day_params).fetchall()

    return {
        "days": days,
        # the window actually applied, so a client can tell a server that ignored start/end
        "range": {"start": start if a is not None else None, "end": end if until_given else None},
        "total_requests": total_reqs,
        # names static/js/usage-report.js reads (kept next to the newer ones)
        "requests": total_reqs,
        "cache_hit_rate": round(prompt_hit_rate * 100, 1),
        "orchestrator_total_tokens": orchestrator_total_tokens,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "avg_tps": round(avg_tps, 2),
        "avg_duration_s": round(avg_duration_s, 2),
        "prompt_cached_tokens": prompt_cached,
        "completion_cached_tokens": completion_cached,
        "total_cached_tokens": total_cached,
        "prompt_hit_rate": round(prompt_hit_rate * 100, 1),
        "completion_hit_rate": round(completion_hit_rate * 100, 1),
        "orchestrator_requests": orchestrator_reqs,
        "by_model": [
            {
                "model": m or "unknown",
                "requests": c,
                "prompt_tokens": p,
                "prompt_cached_tokens": pc,
                "completion_tokens": g,
                "completion_cached_tokens": gc,
                "total_tokens": t,
                "total_cached_tokens": pc + gc,
                "avg_tps": round(s, 2),
                "orchestrator_requests": orc,
                "is_orchestrator": bool(orc or "orchestrator" in (m or "").lower()),
                "source": src,
                "provider": prov,
            }
            for m, c, p, pc, g, gc, t, s, orc, src, prov in by_model_rows
        ],
        "by_day": [
            {
                "day": d,
                "requests": c,
                "prompt_tokens": p,
                "prompt_cached_tokens": pc,
                "completion_tokens": g,
                "completion_cached_tokens": gc,
                "total_tokens": t,
                "total_cached_tokens": pc + gc,
                "orchestrator_requests": orc,
            }
            for d, c, p, pc, g, gc, t, orc in by_day_rows
        ],
    }


def db_report_runs(days: int = 7, limit: int = 10) -> list:
    """The agent runs that used the most prompt tokens in the window, with how each ended: the answer to
    "which task was expensive?". Counts and codes only (no text). Only requests logged with a run_id."""
    since = time.time() - max(1, int(days)) * 86400
    try:
        rows = _usage_db.execute(
            "SELECT q.run_id, MIN(q.ts) AS started, COUNT(*) AS reqs, SUM(q.prompt_tokens) AS prompt, "
            "SUM(q.completion_tokens) AS completion, SUM(q.prompt_cached_tokens) AS cached, MAX(q.prompt_tokens) AS peak, "
            "SUM(CASE WHEN q.source = 'cloud' THEN q.prompt_tokens ELSE 0 END) AS cloud_prompt, "
            "r.steps, r.outcome, r.mode "
            "FROM requests q LEFT JOIN route_runs r ON r.run_id = q.run_id "
            "WHERE q.run_id IS NOT NULL AND q.ts >= ? GROUP BY q.run_id ORDER BY prompt DESC LIMIT ?",
            (since, max(1, min(50, int(limit))))).fetchall()
    except Exception as e:
        print(f"[usage] run report failed: {e}", file=sys.stderr)
        return []
    return [{"run_id": r[0], "started": r[1], "requests": r[2], "prompt_tokens": r[3] or 0,
             "completion_tokens": r[4] or 0, "cached_tokens": r[5] or 0, "peak_prompt_tokens": r[6] or 0,
             "cloud_prompt_tokens": r[7] or 0, "steps": r[8], "outcome": r[9], "mode": r[10]} for r in rows]
