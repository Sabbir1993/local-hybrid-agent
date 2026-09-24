"""core/doc_store.py - identity and version history for generated/edited documents.

Each save of a document is a row in doc_files. An edit never overwrites: it
writes a new file (`report-v2-1a2b3c4d.pptx`) whose parent_id is the version it
came from, so "undo" is simply going back to the parent. `source_spec` keeps
the markdown a PDF was rendered from, which is what PDF edits patch.

location: "common" -> name is a file in the user's COMMON_ROOT/user_<id>/
          "device" -> name is an absolute path on the user's machine
"""

import re
import time
import uuid
from pathlib import Path
from typing import Optional

from .db import _projects_db

_SUFFIX = re.compile(r"(-v\d+)?([-_][0-9a-fA-F]{8})+$")


def base_stem(name: str) -> str:
    """'report-v3-1a2b3c4d.pptx' -> 'report'."""
    return _SUFFIX.sub("", Path(name).stem) or Path(name).stem


def version_name(name: str, version: int) -> str:
    p = Path(name)
    return f"{base_stem(p.name)}-v{version}-{uuid.uuid4().hex[:8]}{p.suffix}"


def register(uid: int, name: str, kind: str, *, location: str = "common", parent_id: Optional[int] = None,
             source_spec: Optional[str] = None, sha256: Optional[str] = None,
             session_id: Optional[int] = None) -> int:
    version = 1
    if parent_id:
        row = get(uid, parent_id)
        version = (row["version"] + 1) if row else 1
    cur = _projects_db.execute(
        "INSERT INTO doc_files (user_id, session_id, name, kind, location, parent_id, version, sha256, "
        "source_spec, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (uid, session_id, name, kind, location, parent_id, version, sha256, source_spec, time.time()))
    _projects_db.commit()
    return cur.lastrowid


def get(uid: int, file_id: int) -> Optional[dict]:
    r = _projects_db.execute("SELECT * FROM doc_files WHERE id = ? AND user_id = ?", (file_id, uid)).fetchone()
    return dict(r) if r else None


def find(uid: int, name: str, location: str = "common") -> Optional[dict]:
    """Latest row for exactly this file name/path (owned by uid)."""
    r = _projects_db.execute(
        "SELECT * FROM doc_files WHERE user_id = ? AND name = ? AND location = ? ORDER BY id DESC LIMIT 1",
        (uid, name, location)).fetchone()
    return dict(r) if r else None


def history(uid: int, file_id: int) -> list[dict]:
    """The version chain ending at file_id, oldest first."""
    out, seen = [], set()
    row = get(uid, file_id)
    while row and row["id"] not in seen:
        seen.add(row["id"])
        out.append(row)
        row = get(uid, row["parent_id"]) if row["parent_id"] else None
    return list(reversed(out))


def audit(uid: int, action: str, name: str, detail: dict) -> None:
    """Audit trail of document edits: op types and hashes only, never content."""
    try:
        from . import auth_db
        auth_db.insert_audit(user_id=uid, username=None, action=action, resource=name,
                             permission_key=None, result="allow",
                             detail=__import__("json").dumps(detail), ip=None)
    except Exception:
        pass
