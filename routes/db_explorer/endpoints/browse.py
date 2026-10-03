import sqlite3
from pathlib import Path
from fastapi import Depends, HTTPException, Query
from core.audit import audit_log
from core.auth import Principal
from core.deps import require_verified
from ..constants import SYSTEM_DBS
from ..helpers import (
    _connect,
    _discover_workspace_dbs,
    _get_table_count,
    _resolve_db,
    _sanitize_cell_value,
)

from .base import router


@router.get("/list")
async def list_databases(user: Principal = Depends(require_verified("database.manage"))):
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

    # 2. This caller's workspace DBs (user-scoped; unscoped call 500s here)
    ws_dbs = _discover_workspace_dbs(user.id)
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
async def get_schema(db_id: str, user: Principal = Depends(require_verified("database.manage"))):
    """Retrieve schema, table list, column types, and DDL for the specified database."""
    path = _resolve_db(db_id, user.id)
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
                columns = []
                try:
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
                except Exception:
                    pass

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


@router.get("/{db_id}/table/{table_name}/data")
async def get_table_data(
    db_id: str,
    table_name: str,
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    user: Principal = Depends(require_verified("database.manage")),
):
    """Quick paginated view of a specific table's contents."""
    path = _resolve_db(db_id, user.id)
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
