"""core/kb_log.py - one row per company-KB retrieval decision: was the KB consulted, why (a fixed reason code), how many
records came back and how well they matched. Codes and counts only, never the question or the records (usage.db is retained
data). It answers "is the gate too strict or too loose?" with numbers instead of anecdotes."""
import sys
import time
from typing import Optional

from .db.usage import _usage_db

RETENTION_DAYS = 90
_last_purge = 0.0


def _init() -> None:
    _usage_db.execute("""CREATE TABLE IF NOT EXISTS kb_decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        user_id INTEGER,
        surface TEXT,
        consulted INTEGER,
        reason TEXT,
        hits INTEGER DEFAULT 0,
        top_cos REAL,
        injected INTEGER DEFAULT 0
    )""")
    _usage_db.execute("CREATE INDEX IF NOT EXISTS idx_kb_decisions_ts ON kb_decisions(ts)")
    _usage_db.commit()


try:
    _init()
except Exception as e:
    print(f"[kb_log] init failed: {e}", file=sys.stderr)


def record(user_id: Optional[int], surface: str, consulted: bool, reason: str, hits: int = 0,
           top_cos: Optional[float] = None, injected: bool = False) -> None:
    """surface: chat | agent. `injected` = the retrieved block was actually put in the prompt (after the score gate)."""
    global _last_purge
    try:
        now = time.time()
        _usage_db.execute(
            "INSERT INTO kb_decisions (ts, user_id, surface, consulted, reason, hits, top_cos, injected) VALUES (?,?,?,?,?,?,?,?)",
            (now, user_id, surface, 1 if consulted else 0, str(reason)[:40], int(hits or 0),
             None if top_cos is None else round(float(top_cos), 3), 1 if injected else 0))
        if now - _last_purge > 86400:
            _last_purge = now
            _usage_db.execute("DELETE FROM kb_decisions WHERE ts < ?", (now - RETENTION_DAYS * 86400,))
        _usage_db.commit()
    except Exception as e:
        print(f"[kb_log] write failed: {e}", file=sys.stderr)


def stats(days: int = 30) -> dict:
    """Decisions per reason code, how often retrieved text was injected, and the score of what was found."""
    since = time.time() - max(1, int(days)) * 86400
    try:
        rows = _usage_db.execute(
            "SELECT reason, consulted, COUNT(*), COALESCE(SUM(injected),0), COALESCE(AVG(top_cos),0) "
            "FROM kb_decisions WHERE ts >= ? GROUP BY reason, consulted ORDER BY COUNT(*) DESC", (since,)).fetchall()
    except Exception as e:
        print(f"[kb_log] stats failed: {e}", file=sys.stderr)
        return {"days": days, "total": 0, "by_reason": []}
    by = [{"reason": r[0], "consulted": bool(r[1]), "count": r[2], "injected": r[3], "avg_top_cos": round(r[4], 3)} for r in rows]
    return {"days": days, "total": sum(b["count"] for b in by), "by_reason": by}
