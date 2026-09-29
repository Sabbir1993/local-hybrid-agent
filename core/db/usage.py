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
        provider TEXT
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
                      provider: Optional[str] = None) -> None:
    """Persist one completed request with input/output cache and orchestrator attribution."""
    try:
        m_lower = (model or "").lower()
        ep_lower = (endpoint or "").lower()
        if not is_orchestrator:
            if "orchestrator" in m_lower or "orchestrator" in ep_lower or ep_lower.startswith("agent/executor") or ep_lower.startswith("agent/needle") or ep_lower.startswith("agent/router"):
                is_orchestrator = True

        _usage_db.execute(
            "INSERT INTO requests (ts, endpoint, model, prompt_tokens, completion_tokens, total_tokens, tps, duration_s, prompt_tps, stream, status, prompt_cached_tokens, completion_cached_tokens, is_orchestrator, source, provider) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), endpoint, model, prompt_tokens or 0, completion_tokens or 0,
             (prompt_tokens or 0) + (completion_tokens or 0), tps, duration_s,
             prompt_tps, 1 if stream else 0, status,
             prompt_cached_tokens or 0, completion_cached_tokens or 0, 1 if is_orchestrator else 0,
             source or "local", provider),
        )
        _usage_db.commit()
    except Exception as e:
        print(f"[server_manager] usage db insert failed: {e}", file=sys.stderr)


def db_report(days: int = 30, model: Optional[str] = None) -> dict:
    """Aggregate token usage report including prompt/completion cache hits and orchestrator counts."""
    since = time.time() - days * 86400
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
            COALESCE(SUM(is_orchestrator), 0)
        FROM requests
        WHERE ts >= ?
    """
    params = [since]
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
        WHERE ts >= ?
        GROUP BY model, COALESCE(source, 'local'), provider
        ORDER BY SUM(total_tokens) DESC
    """
    by_model_rows = _usage_db.execute(by_model_q, [since]).fetchall()

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
        WHERE ts >= ?
    """
    by_day_params = [since]
    if model:
        by_day_q += " AND model = ?"
        by_day_params.append(model)
    by_day_q += " GROUP BY day ORDER BY day ASC"
    by_day_rows = _usage_db.execute(by_day_q, by_day_params).fetchall()

    return {
        "days": days,
        "total_requests": total_reqs,
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
