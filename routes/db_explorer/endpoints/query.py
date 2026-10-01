import sqlite3
import time
from fastapi import Depends, HTTPException
from core.audit import audit_log
from core.auth import Principal
from core.deps import require_permission
from ..constants import QueryRequest, _VACUUM_RE
from ..helpers import _connect, _resolve_db, _sanitize_cell_value

from .base import router


@router.post("/{db_id}/query")
async def execute_query(
    db_id: str,
    body: QueryRequest,
    user: Principal = Depends(require_permission("database.manage")),
):
    """Execute a raw SQL query against the specified database with safety guards and auditing."""
    path = _resolve_db(db_id, user.id)
    sql = body.sql.strip()
    if not sql:
        raise HTTPException(status_code=400, detail="SQL query cannot be empty")

    if _VACUUM_RE.search(sql):
        raise HTTPException(status_code=400, detail="VACUUM is not allowed from the console")
    # Writes are reserved for super admins; everyone else gets a read-only handle.
    writable = user.is_super_admin

    t0 = time.perf_counter()
    is_mutation = False
    affected_rows = 0
    columns = []
    rows_data = []

    try:
        with _connect(path, timeout=10.0, writable=writable) as conn:
            cur = conn.cursor()
            cur.execute(sql)

            if cur.description is not None:
                # Query returned rows (e.g. SELECT, PRAGMA, EXPLAIN)
                columns = [col[0] for col in cur.description]
                fetched = cur.fetchmany(body.max_rows + 1)
                truncated = len(fetched) > body.max_rows
                if truncated:
                    fetched = fetched[:body.max_rows]

                rows_data = [
                    [_sanitize_cell_value(val) for val in row]
                    for row in fetched
                ]
            else:
                # Mutating statement (INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, etc.)
                is_mutation = True
                conn.commit()
                affected_rows = cur.rowcount
                if affected_rows < 0:
                    affected_rows = conn.total_changes

            duration_ms = round((time.perf_counter() - t0) * 1000, 2)

            audit_log(
                user,
                action="database.query",
                resource=db_id,
                permission_key="database.manage",
                result="allow",
                detail={
                    "sql": sql[:400],
                    "mutation": is_mutation,
                    "affected_rows": affected_rows,
                    "row_count": len(rows_data),
                    "duration_ms": duration_ms,
                },
            )

            return {
                "ok": True,
                "db_id": db_id,
                "is_mutation": is_mutation,
                "affected_rows": affected_rows,
                "columns": columns,
                "rows": rows_data,
                "row_count": len(rows_data),
                "truncated": bool(cur.description and len(rows_data) == body.max_rows),
                "duration_ms": duration_ms,
            }

    except sqlite3.Error as e:
        duration_ms = round((time.perf_counter() - t0) * 1000, 2)
        audit_log(
            user,
            action="database.query",
            resource=db_id,
            permission_key="database.manage",
            result="deny",
            detail={"sql": sql[:400], "error": str(e), "duration_ms": duration_ms},
        )
        raise HTTPException(status_code=400, detail=f"SQLite error: {e}")
    except Exception as e:
        duration_ms = round((time.perf_counter() - t0) * 1000, 2)
        audit_log(
            user,
            action="database.query",
            resource=db_id,
            permission_key="database.manage",
            result="deny",
            detail={"sql": sql[:400], "error": str(e), "duration_ms": duration_ms},
        )
        raise HTTPException(status_code=500, detail=f"Execution error: {e}")
