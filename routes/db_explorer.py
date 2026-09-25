"""
routes/db_explorer.py - Web Database Explorer & SQL Query Console.

Permission-restricted via 'database.manage'. Allows administrators to inspect
all system SQLite databases (auth.db, projects.db, usage.db, memory.db) and any
active project workspace databases, view schema and table definitions, and
execute SQL queries with detailed execution metrics and audit logging.
"""

import hashlib
import re
from contextlib import closing
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from core import agent_tools
from core.audit import audit_log
from core.auth import Principal
from core.config import (
    AUTH_DB_FILE,
    BASE_DIR,
    MEMORY_DB_FILE,
    PROJECTS_DB_FILE,
    USAGE_DB_FILE,
)
from core.db import db_list_projects
from core.deps import require_permission

router = APIRouter(prefix="/db", tags=["database"])

# Pre-defined system databases
SYSTEM_DBS = {
    "auth": {
        "id": "auth",
        "name": "auth.db",
        "title": "Authentication & RBAC",
        "description": "Users, roles, permissions, sessions, audit log, and knowledge sources",
        "path": AUTH_DB_FILE,
        "is_system": True,
    },
    "projects": {
        "id": "projects",
        "name": "projects.db",
        "title": "Projects & Chat Sessions",
        "description": "Registered projects, chat sessions, message histories, and plan items",
        "path": PROJECTS_DB_FILE,
        "is_system": True,
    },
    "usage": {
        "id": "usage",
        "name": "usage.db",
        "title": "Usage & Performance Metrics",
        "description": "LLM request metrics, prompt/completion tokens, cache hit metrics, and t/s speed",
        "path": USAGE_DB_FILE,
        "is_system": True,
    },
    "memory": {
        "id": "memory",
        "name": "memory.db",
        "title": "Semantic Vector Memory",
        "description": "Text chunks, cosine embeddings, and past session recall index",
        "path": MEMORY_DB_FILE,
        "is_system": True,
    },
}


class QueryRequest(BaseModel):
    sql: str = Field(..., min_length=1, max_length=65536, description="SQL query to execute")
    max_rows: int = Field(500, ge=1, le=5000, description="Maximum number of rows to return")


def _sanitize_cell_value(val: Any) -> Any:
    """Format cell value for safe JSON serialization."""
    if val is None:
        return None
    if isinstance(val, (int, float, str, bool)):
        return val
    if isinstance(val, bytes):
        if len(val) <= 64:
            return f"0x{val.hex()}"
        return f"<BLOB: {len(val)} bytes, 0x{val[:16].hex()}...>"
    return str(val)


def _discover_workspace_dbs() -> Dict[str, Dict[str, Any]]:
    """Scan active project and registered project directories for SQLite database files."""
    found: Dict[str, Dict[str, Any]] = {}
    searched_dirs = set()

    # 1. Active project workspace
    try:
        active_ws = agent_tools.active_workspace()
        if active_ws and active_ws.exists():
            searched_dirs.add(active_ws.resolve())
    except Exception:
        pass

    # 2. Registered projects in database
    try:
        projects = db_list_projects()
        for p in projects:
            ws = p.get("workspace_dir")
            if ws:
                p_path = Path(ws).resolve()
                if p_path.exists() and p_path.is_dir():
                    searched_dirs.add(p_path)
    except Exception:
        pass

    skip_dirs = {".git", "node_modules", "venv", ".venv", "__pycache__", "build", "dist", ".cache"}
    exts = {".db", ".sqlite", ".sqlite3"}

    for base_dir in searched_dirs:
        try:
            for root, dirs, files in os.walk(base_dir):
                # Prune skipped directories
                dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]
                for file in files:
                    p = Path(root) / file
                    if p.suffix.lower() in exts:
                        # Make sure it's not one of our system DBs
                        resolved = p.resolve()
                        if any(resolved == s["path"].resolve() for s in SYSTEM_DBS.values()):
                            continue
                        rel = str(resolved)
                        h = hashlib.sha256(rel.encode("utf-8")).hexdigest()[:10]
                        ws_id = f"ws_{h}"
                        found[ws_id] = {
                            "id": ws_id,
                            "name": p.name,
                            "title": f"Workspace: {p.name}",
                            "description": f"Located at {rel}",
                            "path": resolved,
                            "is_system": False,
                        }
        except Exception:
            continue

    return found


def _resolve_db(db_id: str) -> Path:
    """Resolve database ID to verified path, protecting against path traversal."""
    if db_id in SYSTEM_DBS:
        p = SYSTEM_DBS[db_id]["path"]
        if not p.exists():
            # Auto-touch/create if missing
            p.parent.mkdir(parents=True, exist_ok=True)
            p.touch(exist_ok=True)
        return p

    ws_dbs = _discover_workspace_dbs()
    if db_id in ws_dbs:
        p = ws_dbs[db_id]["path"]
        if p.exists() and p.is_file():
            return p

    raise HTTPException(status_code=404, detail=f"Database '{db_id}' not found")


# auth.db columns nobody should read through the console: credential hashes
# and live session ids (the sha256 is enough to hijack if the DB is copied).
_HIDDEN_COLUMNS = {("users", "password_hash"), ("auth_sessions", "id")}
# Introspection pragmas take a table/index argument; settable ones must be bare (no "= v").
_INTROSPECT_PRAGMAS = {"table_info", "table_xinfo", "index_list", "index_info", "index_xinfo",
                       "foreign_key_list", "database_list", "compile_options", "page_count"}
_SETTABLE_PRAGMAS = {"page_size", "user_version", "schema_version", "journal_mode"}
_VACUUM_RE = re.compile(r"\bVACUUM\b", re.IGNORECASE)


def _authorizer(hide_auth_columns: bool):
    def _auth(action, arg1, arg2, _db, _trigger):
        if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_PRAGMA:
            name = (arg1 or "").lower()
            if not (name in _INTROSPECT_PRAGMAS or (name in _SETTABLE_PRAGMAS and arg2 is None)):
                return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() == "load_extension":
            return sqlite3.SQLITE_DENY
        if hide_auth_columns and action == sqlite3.SQLITE_READ and (arg1, arg2) in _HIDDEN_COLUMNS:
            return sqlite3.SQLITE_IGNORE   # column reads back as NULL
        return sqlite3.SQLITE_OK
    return _auth


def _connect(path: Path, timeout: float, writable: bool = False):
    """Open a console connection: read-only unless explicitly writable, with
    ATTACH / PRAGMA writes / load_extension blocked either way. auth.db is
    never writable -- it holds the audit log (PCI DSS 10.3.2)."""
    writable = writable and path.resolve() != Path(AUTH_DB_FILE).resolve()
    uri = path.resolve().as_uri() + ("" if writable else "?mode=ro")
    conn = sqlite3.connect(uri, uri=True, timeout=timeout)
    conn.set_authorizer(_authorizer(path.resolve() == Path(AUTH_DB_FILE).resolve()))
    return closing(conn)


def _get_table_count(path: Path) -> int:
    """Count user tables in database."""
    try:
        with _connect(path, timeout=3.0) as conn:
            cur = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%'"
            )
            return int(cur.fetchone()[0])
    except Exception:
        return 0


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
