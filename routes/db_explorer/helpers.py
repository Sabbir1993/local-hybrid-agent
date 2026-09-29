"""
routes/db_explorer/helpers.py - Utility functions for the DB Explorer.
"""

import hashlib
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Dict

from fastapi import HTTPException

from core import agent_tools
from core.config import AUTH_DB_FILE
from core.db import db_list_projects

from .constants import (
    SYSTEM_DBS,
    _HIDDEN_COLUMNS,
    _INTROSPECT_PRAGMAS,
    _SETTABLE_PRAGMAS,
)


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
