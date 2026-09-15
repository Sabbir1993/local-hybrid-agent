"""
routes/projects.py - Projects, sessions, messages, and filesystem browsing endpoints.
"""

import asyncio
import os
import string
import subprocess
import sys
from pathlib import Path
from typing import Optional, Union

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.small_model import WORKSPACE_ROOT
from core.db import (
    db_list_projects,
    db_create_project,
    db_delete_project,
    db_list_sessions,
    db_create_session,
    db_update_session_title,
    db_delete_session,
    db_load_messages,
    db_append_message,
)
from core.agent_tools import (
    active_workspace,
    get_active_project,
    set_active_project,
)

router = APIRouter(tags=["projects"])

class ProjectReq(BaseModel):
    name: str
    workspace_dir: Optional[str] = None


class SessionReq(BaseModel):
    title: Optional[str] = None


class BrowseFolderReq(BaseModel):
    initial_dir: Optional[str] = ""


class MkdirReq(BaseModel):
    path: str
    name: str


def _get_drives() -> list:
    drives = []
    if sys.platform == "win32":
        try:
            from ctypes import windll
            bitmask = windll.kernel32.GetLogicalDrives()
            for letter in string.ascii_uppercase:
                if bitmask & 1:
                    d = f"{letter}:\\"
                    if os.path.exists(d):
                        drives.append(d)
                bitmask >>= 1
        except Exception as e:
            print(f"[server_manager] Failed to query logical drives: {e}", file=sys.stderr)
    if not drives:
        drives = ["E:\\", "C:\\"] if sys.platform == "win32" else ["/"]
    return drives


def _ask_directory_native(initial_dir: str = "") -> str:
    init_dir = (initial_dir or "").strip()
    if not init_dir or not os.path.isdir(init_dir):
        if os.path.isdir("E:\\AI"):
            init_dir = "E:\\AI"
        elif WORKSPACE_ROOT and os.path.isdir(str(WORKSPACE_ROOT)):
            init_dir = str(WORKSPACE_ROOT)
        else:
            init_dir = os.path.expanduser("~")

    try:
        py_script = f"""
import sys, os
try:
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    root.lift()
    root.focus_force()
    res = filedialog.askdirectory(title='Select Local Workspace Directory', initialdir={repr(init_dir)}, mustexist=False)
    root.destroy()
    if res:
        print(os.path.normpath(res))
except Exception:
    sys.exit(1)
"""
        proc = subprocess.run(
            [sys.executable, "-c", py_script],
            capture_output=True,
            text=True,
            timeout=120
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return os.path.normpath(proc.stdout.strip())
    except Exception as e:
        print(f"[server_manager] tkinter subprocess failed ({e}), trying PowerShell fallback", file=sys.stderr)

    try:
        escaped_init = init_dir.replace("'", "''")
        ps_cmd = (
            "[System.Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms') | Out-Null; "
            "$f = New-Object System.Windows.Forms.FolderBrowserDialog; "
            "$f.Description = 'Select Local Workspace Directory'; "
            "$f.ShowNewFolderButton = $true; "
            f"$f.SelectedPath = '{escaped_init}'; "
            "if ($f.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { "
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
            "Write-Output $f.SelectedPath }"
        )
        res = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=120
        )
        if res.returncode == 0 and res.stdout.strip():
            return os.path.normpath(res.stdout.strip())
    except Exception as e:
        print(f"[server_manager] PowerShell folder dialog failed: {e}", file=sys.stderr)

    return ""


@router.post("/control/browse_folder")
async def browse_folder(req: Optional[BrowseFolderReq] = None):
    init_dir = req.initial_dir if req else ""
    try:
        selected_path = await asyncio.to_thread(_ask_directory_native, init_dir)
        return {
            "ok": True,
            "path": selected_path or "",
            "cancelled": not bool(selected_path),
        }
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.get("/control/fs/browse")
async def fs_browse(path: Optional[str] = ""):
    drives = _get_drives()
    raw_path = (path or "").strip()
    if not raw_path:
        if os.path.isdir("E:\\AI"):
            target_path = Path("E:\\AI")
        elif WORKSPACE_ROOT and os.path.isdir(str(WORKSPACE_ROOT)):
            target_path = Path(WORKSPACE_ROOT)
        elif drives:
            target_path = Path(drives[0])
        else:
            target_path = Path(os.path.expanduser("~"))
    else:
        target_path = Path(raw_path).expanduser().resolve()

    if not target_path.exists() or not target_path.is_dir():
        if target_path.parent.exists() and target_path.parent.is_dir():
            target_path = target_path.parent
        else:
            target_path = Path("E:\\AI") if os.path.isdir("E:\\AI") else Path(str(WORKSPACE_ROOT))

    subdirs = []
    try:
        with os.scandir(str(target_path)) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        name = entry.name
                        if not name.startswith(('.', '$')) and name.lower() not in (
                            'system volume information', 'recovery', '$recycle.bin'
                        ):
                            subdirs.append(name)
                except (PermissionError, OSError):
                    continue
    except (PermissionError, OSError) as e:
        print(f"[server_manager] fs_browse scan error on {target_path}: {e}", file=sys.stderr)

    subdirs.sort(key=lambda s: s.lower())
    parent_dir = str(target_path.parent) if target_path.parent != target_path else None

    return {
        "ok": True,
        "current": str(target_path),
        "parent": parent_dir,
        "drives": drives,
        "subdirs": subdirs[:250],
    }


@router.post("/control/fs/mkdir")
async def fs_mkdir(req: MkdirReq):
    try:
        base = Path(req.path).expanduser().resolve()
        name = req.name.strip()
        if not name or any(c in name for c in '<>:"/\\|?*'):
            return JSONResponse({"ok": False, "error": "Invalid folder name"}, status_code=400)
        target = base / name
        target.mkdir(parents=True, exist_ok=True)
        return {"ok": True, "path": str(target.resolve())}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


class MessageReq(BaseModel):
    role: str
    content: str
    meta: Optional[dict] = None


@router.get("/control/projects")
async def list_projects():
    projs = db_list_projects()
    active_p = None
    curr_proj = get_active_project()
    if curr_proj:
        for p in projs:
            if p["name"] == curr_proj:
                active_p = p
                break
    return {
        "projects": projs,
        "active": curr_proj,
        "active_project": active_p,
        "workspace": str(active_workspace()),
        "workspace_root": str(WORKSPACE_ROOT),
    }


@router.post("/control/projects")
async def create_project(req: ProjectReq):
    try:
        p = db_create_project(req.name, req.workspace_dir, WORKSPACE_ROOT)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "project": p}


@router.delete("/control/projects/{pid}")
async def delete_project(pid: int):
    pname = None
    for p in db_list_projects():
        if p["id"] == pid:
            pname = p["name"]
            break
    db_delete_project(pid)
    curr_proj = get_active_project()
    if curr_proj:
        projs = db_list_projects()
        if not any(p["name"] == curr_proj for p in projs):
            set_active_project(None)
    return {"ok": True, "deleted_id": pid, "deleted_name": pname}


@router.post("/control/projects/{pid}/activate")
async def activate_project(pid: int):
    if pid == 0:
        set_active_project(None)
        return {"ok": True, "active": None, "workspace": str(active_workspace())}
    for p in db_list_projects():
        if p["id"] == pid:
            set_active_project(p["name"])
            return {"ok": True, "active": p["name"], "project": p, "workspace": str(active_workspace())}
    return JSONResponse({"error": "project not found"}, status_code=404)


@router.get("/control/projects/{pid}/sessions")
async def list_sessions(pid: int):
    return {"sessions": db_list_sessions(pid)}


@router.post("/control/projects/{pid}/sessions")
async def create_session(pid: int, req: SessionReq):
    try:
        s = db_create_session(pid, req.title)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "session": s}


@router.patch("/control/sessions/{sid}")
async def update_session(sid: int, req: SessionReq):
    try:
        db_update_session_title(sid, req.title)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True}


@router.delete("/control/sessions/{sid}")
async def delete_session(sid: int):
    db_delete_session(sid)
    return {"ok": True}


@router.get("/control/sessions/{sid}/messages")
async def get_messages(sid: int):
    return {"messages": db_load_messages(sid)}


@router.post("/control/sessions/{sid}/messages")
async def post_message(sid: int, req: MessageReq):
    try:
        mid = db_append_message(sid, req.role, req.content, req.meta)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "id": mid}



