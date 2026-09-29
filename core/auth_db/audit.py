import sys
import time
from typing import Optional

from .common import db


def insert_audit(user_id: Optional[int], username: Optional[str], action: str,
                 resource: Optional[str], permission_key: Optional[str],
                 result: str, detail: Optional[str], ip: Optional[str]) -> None:
    try:
        db().execute(
            "INSERT INTO audit_log (ts, user_id, username, action, resource, permission_key, result, detail, ip) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), user_id, username, action, resource, permission_key, result, detail, ip),
        )
        db().commit()
    except Exception as e:
        print(f"[auth_db] audit insert failed: {e}", file=sys.stderr)


def list_audit(since: float = 0.0, user_id: Optional[int] = None, limit: int = 500) -> list:
    q = "SELECT * FROM audit_log WHERE ts >= ?"
    args: list = [since]
    if user_id is not None:
        q += " AND user_id = ?"
        args.append(user_id)
    q += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in db().execute(q, args)]


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _audit_where(since: float = 0.0, until: Optional[float] = None, user_id: Optional[int] = None,
                 action: Optional[str] = None, result: Optional[str] = None,
                 ip: Optional[str] = None, text: Optional[str] = None) -> tuple[str, list]:
    """WHERE clause for audit filters; every value is bound, never interpolated."""
    clauses, args = ["ts >= ?"], [since or 0.0]
    if until:
        clauses.append("ts < ?")
        args.append(until)
    if user_id is not None:
        # 0 = system/anonymous entries (no user attached)
        if user_id == 0:
            clauses.append("user_id IS NULL")
        else:
            clauses.append("user_id = ?")
            args.append(user_id)
    if action:
        # "users.*" matches every action under that prefix
        if action.endswith(".*"):
            clauses.append("action LIKE ? ESCAPE '\\'")
            args.append(_like_escape(action[:-1]) + "%")
        else:
            clauses.append("action = ?")
            args.append(action)
    if result:
        clauses.append("result = ?")
        args.append(result)
    if ip:
        clauses.append("ip LIKE ? ESCAPE '\\'")
        args.append(_like_escape(ip) + "%")
    if text:
        pat = "%" + _like_escape(text) + "%"
        clauses.append("(resource LIKE ? ESCAPE '\\' OR detail LIKE ? ESCAPE '\\' OR action LIKE ? ESCAPE '\\' "
                       "OR permission_key LIKE ? ESCAPE '\\' OR username LIKE ? ESCAPE '\\')")
        args += [pat] * 5
    return " WHERE " + " AND ".join(clauses), args


def query_audit(page: int = 1, page_size: int = 50, **filters) -> dict:
    """One page of audit entries (newest first) plus the total matching count."""
    where, args = _audit_where(**filters)
    total = db().execute("SELECT COUNT(*) FROM audit_log" + where, args).fetchone()[0]
    rows = db().execute(
        "SELECT * FROM audit_log" + where + " ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
        args + [page_size, (page - 1) * page_size],
    )
    return {"entries": [dict(r) for r in rows], "total": total}


def iter_audit(max_rows: int = 50000, **filters) -> list:
    """All entries matching the filters (newest first), capped — for CSV export."""
    where, args = _audit_where(**filters)
    q = "SELECT * FROM audit_log" + where + " ORDER BY ts DESC, id DESC LIMIT ?"
    return [dict(r) for r in db().execute(q, args + [max_rows])]


def audit_facets() -> dict:
    """Distinct users / actions / results present in the log, for filter dropdowns."""
    users = [dict(r) for r in db().execute(
        "SELECT user_id, MAX(username) AS username, COUNT(*) AS n FROM audit_log "
        "GROUP BY user_id ORDER BY username COLLATE NOCASE")]
    actions = [dict(r) for r in db().execute(
        "SELECT action, COUNT(*) AS n FROM audit_log GROUP BY action ORDER BY action")]
    results = [r[0] for r in db().execute(
        "SELECT DISTINCT result FROM audit_log ORDER BY result")]
    return {"users": users, "actions": actions, "results": results}
