"""
routes/db_explorer/endpoints.py - FastAPI route handlers for the DB Explorer.

Permission-restricted via 'database.manage'. Allows administrators to inspect
all system SQLite databases (auth.db, projects.db, usage.db, memory.db) and any
active project workspace databases, view schema and table definitions, and
execute SQL queries with detailed execution metrics and audit logging.
"""

import sqlite3
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query

from core.audit import audit_log
from core.auth import Principal
from core.deps import require_permission

from .constants import (
    SYSTEM_DBS,
    QueryRequest,
    _VACUUM_RE,
)
from .helpers import (
    _connect,
    _discover_workspace_dbs,
    _get_table_count,
    _resolve_db,
    _sanitize_cell_value,
)

router = APIRouter(prefix="/db", tags=["database"])


@router.get("/list")
async def list_databases(user: Principal = Depends(require_permission("database.manage"))):
    """List all available system and workspace SQLite databases."""
    out = []
    # 1. System DBs
    for d in SYSTEM_DBS.values():
        p: Path = d["path"]
        size = p.stat().st_size if p.exists() else 0
        t_count = _get_table_count(p) if p.exists() else 0
        out.append({
            "id": d["id"],
            "name": d["name"],
            "title": d["title"],
            "description": d["description"],
            "path": str(p),
            "size_bytes": size,
            "table_count": t_count,
            "is_system": True,
        })

    # 2. Workspace DBs
    ws_dbs = _discover_workspace_dbs()
    for d in ws_dbs.values():
        p: Path = d["path"]
        size = p.stat().st_size if p.exists() else 0
        t_count = _get_table_count(p) if p.exists() else 0
        out.append({
            "id": d["id"],
            "name": d["name"],
            "title": d["title"],
            "description": d["description"],
            "path": str(p),
            "size_bytes": size,
            "table_count": t_count,
            "is_system": False,
        })

    return {"databases": out}


@router.get("/{db_id}/schema")
async def get_schema(db_id: str, user: Principal = Depends(require_permission("database.manage"))):
    """Retrieve schema, table list, column types, and DDL for the specified database."""
    path = _resolve_db(db_id)
    tables = []

    try:
        with _connect(path, timeout=5.0) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' "
                "ORDER BY type, name"
            ).fetchall()

            for r in rows:
                tbl_name = r["name"]
                tbl_type = r["type"]
                sql = r["sql"] or ""

                # Get columns
                col_rows = conn.execute(f"PRAGMA table_info(\"{tbl_name}\")").fetchall()
                columns = [
                    {
                        "cid": c["cid"],
                        "name": c["name"],
                        "type": c["type"] or "TEXT",
                        "notnull": bool(c["notnull"]),
                        "dflt_value": c["dflt_value"],
                        "pk": bool(c["pk"]),
                    }
                    for c in col_rows
                ]

                # Estimated count
                row_count = 0
                if tbl_type == "table":
                    try:
                        cnt_row = conn.execute(f"SELECT COUNT(*) as c FROM \"{tbl_name}\"").fetchone()
                        if cnt_row:
                            row_count = int(cnt_row["c"])
                    except Exception:
                        row_count = -1

                tables.append({
                    "name": tbl_name,
                    "type": tbl_type,
                    "row_count": row_count,
                    "columns": columns,
                    "sql": sql,
                })

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read database schema: {e}")

    return {
        "db_id": db_id,
        "path": str(path),
        "tables": tables,
    }


@router.post("/{db_id}/query")
async def execute_query(
    db_id: str,
    body: QueryRequest,
    user: Principal = Depends(require_permission("database.manage")),
):
    """Execute a raw SQL query against the specified database with safety guards and auditing."""
    path = _resolve_db(db_id)
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


@router.get("/{db_id}/table/{table_name}/data")
async def get_table_data(
    db_id: str,
    table_name: str,
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    user: Principal = Depends(require_permission("database.manage")),
):
    """Quick paginated view of a specific table's contents."""
    path = _resolve_db(db_id)
    audit_log(user, action="database.read", resource=f"{db_id}:{table_name}", permission_key="database.manage",
              detail={"limit": limit, "offset": offset})

    try:
        with _connect(path, timeout=5.0) as conn:
            # Verify table exists in sqlite_master
            check = conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
                (table_name,),
            ).fetchone()
            if not check:
                raise HTTPException(status_code=404, detail=f"Table '{table_name}' not found")

            # Get total count
            total = 0
            try:
                cnt = conn.execute(f"SELECT COUNT(*) FROM \"{table_name}\"").fetchone()
                total = int(cnt[0]) if cnt else 0
            except Exception:
                total = 0

            # Fetch paginated rows
            cur = conn.execute(f"SELECT * FROM \"{table_name}\" LIMIT ? OFFSET ?", (limit, offset))
            columns = [c[0] for c in cur.description] if cur.description else []
            rows = [
                [_sanitize_cell_value(v) for v in row]
                for row in cur.fetchall()
            ]

            return {
                "db_id": db_id,
                "table": table_name,
                "columns": columns,
                "rows": rows,
                "total": total,
                "limit": limit,
                "offset": offset,
            }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to query table: {e}")
