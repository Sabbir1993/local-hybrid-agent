import os
import sys
from pathlib import Path
from typing import Optional

from .. import companion_bridge
from ..db import _projects_db
from ..request_context import get_current_device_id, get_current_user_id, personal_workspace
from ..small_model import COMMON_ROOT

MAX_TOOL_OUTPUT = 20000
MAX_EDIT_BYTES = 512 * 1024
DOCUMENT_EXTS = (".xlsx", ".xls", ".pptx", ".ppt", ".docx", ".doc", ".pdf")

_active_project: dict = {}
_ws_changes: dict = {}


def _user_device_key(user_id: Optional[int] = None, device_id: Optional[str] = None) -> str:
    uid = user_id if user_id is not None else get_current_user_id()
    did = device_id if device_id is not None else get_current_device_id()
    return f"{uid}:{did or 'default'}"


def get_active_project(user_id: Optional[int] = None, device_id: Optional[str] = None) -> Optional[str]:
    k = _user_device_key(user_id, device_id)
    if k in _active_project:
        return _active_project[k]
    uid = user_id if user_id is not None else get_current_user_id()
    return _active_project.get(f"{uid}:default") or _active_project.get(uid)


def set_active_project(name: Optional[str], user_id: Optional[int] = None, device_id: Optional[str] = None) -> None:
    k = _user_device_key(user_id, device_id)
    _active_project[k] = name
    _ws_changes.pop(user_id if user_id is not None else get_current_user_id(), None)


class WorkspaceAccessDenied(PermissionError):
    """Agent workspace access refused: the server's own disk is never a workspace."""


def _pkg():
    return sys.modules.get("core.agent_tools")


def require_device_workspace() -> tuple[int, Path]:
    """(user_id, project folder on the user's machine) for the calling request."""
    uid = get_current_user_id()
    did = get_current_device_id()
    if uid is None:
        raise WorkspaceAccessDenied("not signed in")
    own = personal_workspace()
    if own:
        # a Personal Agent's own folder (set on the agent) stands in for the project folder
        cb = getattr(_pkg(), "companion_bridge", companion_bridge)
        if not cb.is_available(uid):
            raise WorkspaceAccessDenied(
                "the SSL Local Agent app is not connected - open it on your machine and try again")
        return uid, Path(own)
    proj = _active_project.get(_user_device_key(uid, did))
    if not proj:
        raise WorkspaceAccessDenied(
            "no project is selected on this device - select a project from your machine first")
    db = getattr(_pkg(), "_projects_db", _projects_db)
    row = db.execute(
        "SELECT workspace_dir FROM projects WHERE name = ? AND user_id = ? AND device_id = ?",
        (proj, uid, did)).fetchone()
    if not row or not row["workspace_dir"]:
        raise WorkspaceAccessDenied(
            f"project '{proj}' has no folder on your machine - recreate it and pick a local folder")
    cb = getattr(_pkg(), "companion_bridge", companion_bridge)
    if not cb.is_available(uid):
        raise WorkspaceAccessDenied(
            "the SSL Local Agent app is not connected - open it on your machine and try again")
    comp_dev = (cb.connection_info(uid) or {}).get("device_id")
    if comp_dev and comp_dev != did:
        raise WorkspaceAccessDenied(
            "the connected SSL Local Agent is on a different machine than this project")
    return uid, Path(row["workspace_dir"])


def active_workspace() -> Path:
    """The active project's folder on the user's machine (see require_device_workspace)."""
    req_fn = getattr(_pkg(), "require_device_workspace", require_device_workspace)
    return req_fn()[1]


def workspace_label() -> Optional[str]:
    """Display-only: the device workspace path, or None when unavailable (never raises)."""
    req_fn = getattr(_pkg(), "require_device_workspace", require_device_workspace)
    try:
        return str(req_fn()[1])
    except WorkspaceAccessDenied:
        return None


def _remote_uid() -> int:
    """user_id whose companion executes workspace ops."""
    req_fn = getattr(_pkg(), "require_device_workspace", require_device_workspace)
    return req_fn()[0]


def user_common_root(uid: int) -> Path:
    """COMMON_ROOT/user_<uid>: one user's generated files and uploads."""
    cr = getattr(_pkg(), "COMMON_ROOT", COMMON_ROOT)
    return cr.resolve() / f"user_{int(uid)}"


def common_workspace() -> Path:
    """The calling user's private common space."""
    uid = get_current_user_id()
    if uid is None:
        raise PermissionError("not signed in")
    p = user_common_root(uid)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _within(p: Path, root: Path) -> bool:
    try:
        p.relative_to(root)
        return True
    except ValueError:
        pass
    rp = os.path.normcase(os.path.normpath(str(p)))
    rr = os.path.normcase(os.path.normpath(str(root)))
    return rp == rr or rp.startswith(rr.rstrip("/" + os.sep) + os.sep)


def _common_resolve(rel: str) -> Path:
    common = common_workspace()
    clean = str(rel).strip().replace("\\", "/")

    common_str = str(common).replace("\\", "/")
    if clean.lower().startswith(common_str.lower()):
        clean = clean[len(common_str):].lstrip("/")

    prefixes = ["/common/", "common/", "/workspace/", "workspace/"]
    for prefix in prefixes:
        if clean.lower().startswith(prefix):
            clean = clean[len(prefix):]
            break

    clean = clean.lstrip("/\\")
    if not clean or clean == ".":
        return common

    p = (common / clean).resolve()
    if not _within(p, common.resolve()):
        raise PermissionError(f"path escapes common space: {rel}")
    return p


def _ws_resolve(rel: str) -> Path:
    ws_fn = getattr(_pkg(), "active_workspace", active_workspace)
    ws = ws_fn().resolve()
    clean = str(rel).strip().replace("\\", "/")

    ws_str = str(ws).replace("\\", "/")
    if clean.lower().startswith(ws_str.lower()):
        clean = clean[len(ws_str):].lstrip("/")

    prefixes = ["/workspace/", "workspace/", "/root/workspace/", "./workspace/"]
    for prefix in prefixes:
        if clean.lower().startswith(prefix):
            clean = clean[len(prefix):]
            break

    clean = clean.lstrip("/\\")
    if not clean or clean == ".":
        return ws

    p = (ws / clean).resolve()
    if not _within(p, ws):
        raise PermissionError(f"path escapes workspace: {rel}")
    return p
