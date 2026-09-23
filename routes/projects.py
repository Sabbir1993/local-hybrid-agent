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

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.auth import Principal
from core.deps import get_current_user
from core.request_context import set_current_device, get_current_device_id
from core.db import (
    db_list_projects,
    db_create_project,
    db_update_project_workspace,
    db_delete_project,
    db_list_sessions,
    db_create_session,
    db_update_session_title,
    db_delete_session,
    db_load_messages,
    db_append_message,
    db_rename_device,
    db_list_user_devices,
)
from core.agent_tools import (
    workspace_label,
    get_active_project,
    set_active_project,
)
from core import companion_bridge

router = APIRouter(tags=["projects"])


@router.get("/control/companion/status")
async def companion_status(user: Principal = Depends(get_current_user)):
    info = companion_bridge.connection_info(user.id)
    return {"connected": info is not None, **(info or {})}

class ProjectReq(BaseModel):
    name: str
    workspace_dir: Optional[str] = None
    device_id: Optional[str] = None
    device_name: Optional[str] = None


class UpdateWorkspaceReq(BaseModel):
    workspace_dir: str
    device_id: Optional[str] = None
    device_name: Optional[str] = None


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
async def browse_folder(request: Request,
                        req: Optional[BrowseFolderReq] = None,
                        user: Principal = Depends(get_current_user)):
    init_dir = req.initial_dir if req else ""
    if companion_bridge.is_connected(user.id):
        try:
            data = await companion_bridge.call(user.id, "fs.browse_folder", {"initial_dir": init_dir})
            return {"ok": True, "path": data.get("path") or "", "cancelled": not bool(data.get("path"))}
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"companion: {e}"}, status_code=502)

    # If companion is not connected, check if client is physically on localhost.
    # NEVER open a server-side GUI dialog for remote users!
    client_ip = request.client.host if (request and request.client) else ""
    is_localhost = client_ip in ("127.0.0.1", "::1", "localhost", "testclient")
    if not is_localhost:
        return JSONResponse({
            "ok": False,
            "error": "No companion connected on your machine. Please type or paste your local folder path directly.",
            "is_remote": True,
        }, status_code=400)

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
async def fs_browse(request: Request, path: Optional[str] = "", user: Principal = Depends(get_current_user)):
    if companion_bridge.is_connected(user.id):
        try:
            data = await companion_bridge.call(user.id, "fs.browse", {"path": path or ""})
            return {"ok": True, **data}
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"companion: {e}"}, status_code=502)

    client_ip = request.client.host if (request and request.client) else ""
    is_localhost = client_ip in ("127.0.0.1", "::1", "localhost", "testclient")
    if not is_localhost:
        return JSONResponse({
            "ok": False,
            "error": "In-app filesystem browsing requires the local companion app on your machine.",
            "is_remote": True,
        }, status_code=400)

    drives = _get_drives()
    raw_path = (path or "").strip()
    if not raw_path:
        if os.path.isdir("E:\\AI"):
            target_path = Path("E:\\AI")
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
            target_path = Path("E:\\AI") if os.path.isdir("E:\\AI") else Path(os.path.expanduser("~"))

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
async def fs_mkdir(req: MkdirReq, user: Principal = Depends(get_current_user)):
    if companion_bridge.is_connected(user.id):
        try:
            data = await companion_bridge.call(user.id, "fs.mkdir", {"path": req.path, "name": req.name})
            return {"ok": True, **data}
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"companion: {e}"}, status_code=502)
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


class RenameDeviceReq(BaseModel):
    new_name: str
    old_name: Optional[str] = None
    device_id: Optional[str] = None


@router.post("/control/device/rename")
async def rename_device(req: RenameDeviceReq,
                        user: Principal = Depends(get_current_user),
                        x_device_id: Optional[str] = Header(None, alias="X-Device-Id")):
    new_name = req.new_name.strip()
    if not new_name:
        return JSONResponse({"error": "Device name cannot be empty"}, status_code=400)
    dev_id = req.device_id or x_device_id
    db_rename_device(owner_user_id=user.id, new_name=new_name, device_id=dev_id, old_name=req.old_name)
    set_current_device(dev_id or "default", new_name)
    return {"ok": True, "device_name": new_name}


@router.get("/control/user/devices")
async def list_user_devices(user: Principal = Depends(get_current_user)):
    return {"ok": True, "devices": db_list_user_devices(user.id)}


@router.get("/control/projects")
async def list_projects(user: Principal = Depends(get_current_user),
                        device: Optional[str] = "current",
                        device_id: Optional[str] = None,
                        x_device_id: Optional[str] = Header(None, alias="X-Device-Id"),
                        x_device_name: Optional[str] = Header(None, alias="X-Device-Name")):
    cur_dev_id = device_id or x_device_id or "default"
    set_current_device(cur_dev_id, x_device_name)

    # Filter by current device and device name.
    # NOTE: do NOT fall back to "all projects for user" if the device filter matches 0 —
    # that would leak projects registered on other machines (e.g. office PC) onto this device.
    filter_dev_id = cur_dev_id if (device != "all" and cur_dev_id != "default") else None
    filter_dev_name = x_device_name if (device != "all") else None
    projs = db_list_projects(owner_user_id=user.id, device_id=filter_dev_id, device_name=filter_dev_name)

    curr_proj = get_active_project(user.id, cur_dev_id)
    active_p = None

    for p in projs:
        p_dev = p.get("device_id")
        p["is_current_device"] = bool(p_dev == cur_dev_id or p_dev in ("default", None) or cur_dev_id == "default")
        # the folder lives on the user's machine; probing it on the server's
        # disk would be both wrong and a server filesystem oracle
        p["path_valid_on_device"] = True

        if curr_proj and p["name"] == curr_proj and (p_dev == cur_dev_id or cur_dev_id == "default" or p_dev in ("default", None)):
            active_p = p

    return {
        "projects": projs,
        "active": curr_proj,
        "active_project": active_p,
        "workspace": workspace_label(),
        "current_device_id": cur_dev_id,
        "current_device_name": x_device_name or "Default Device",
    }


@router.post("/control/projects")
async def create_project(req: ProjectReq,
                         user: Principal = Depends(get_current_user),
                         x_device_id: Optional[str] = Header(None, alias="X-Device-Id"),
                         x_device_name: Optional[str] = Header(None, alias="X-Device-Name")):
    dev_id = req.device_id or x_device_id or "default"
    dev_name = req.device_name or x_device_name or "Default Device"
    set_current_device(dev_id, dev_name)
    try:
        p = db_create_project(req.name, req.workspace_dir, owner_user_id=user.id,
                              device_id=dev_id, device_name=dev_name)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "project": p}


@router.patch("/control/projects/{pid}/workspace")
async def update_project_workspace(pid: int, req: UpdateWorkspaceReq,
                                  user: Principal = Depends(get_current_user),
                                  x_device_id: Optional[str] = Header(None, alias="X-Device-Id"),
                                  x_device_name: Optional[str] = Header(None, alias="X-Device-Name")):
    dev_id = req.device_id or x_device_id or "default"
    dev_name = req.device_name or x_device_name or "Default Device"
    set_current_device(dev_id, dev_name)
    try:
        p = db_update_project_workspace(pid, req.workspace_dir, user.id, dev_id, dev_name)
        return {"ok": True, "project": p}
    except PermissionError:
        return JSONResponse({"error": "project not found"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@router.delete("/control/projects/{pid}")
async def delete_project(pid: int, user: Principal = Depends(get_current_user),
                         x_device_id: Optional[str] = Header(None, alias="X-Device-Id")):
    dev_id = x_device_id or "default"
    pname = None
    for p in db_list_projects(owner_user_id=user.id):
        if p["id"] == pid:
            pname = p["name"]
            break
    try:
        db_delete_project(pid, owner_user_id=user.id)
    except PermissionError:
        return JSONResponse({"error": "project not found"}, status_code=404)
    curr_proj = get_active_project(user.id, dev_id)
    if curr_proj:
        projs = db_list_projects(owner_user_id=user.id)
        if not any(p["name"] == curr_proj for p in projs):
            set_active_project(None, user.id, dev_id)
    return {"ok": True, "deleted_id": pid, "deleted_name": pname}


@router.post("/control/projects/{pid}/activate")
async def activate_project(pid: int, user: Principal = Depends(get_current_user),
                           x_device_id: Optional[str] = Header(None, alias="X-Device-Id"),
                           x_device_name: Optional[str] = Header(None, alias="X-Device-Name")):
    dev_id = x_device_id or "default"
    set_current_device(dev_id, x_device_name)
    if pid == 0:
        set_active_project(None, user.id, dev_id)
        return {"ok": True, "active": None, "workspace": workspace_label()}
    for p in db_list_projects(owner_user_id=user.id):
        if p["id"] == pid:
            set_active_project(p["name"], user.id, dev_id)
            return {"ok": True, "active": p["name"], "project": p, "workspace": workspace_label()}
    return JSONResponse({"error": "project not found"}, status_code=404)


@router.get("/control/projects/{pid}/sessions")
async def list_sessions(pid: int, user: Principal = Depends(get_current_user)):
    try:
        return {"sessions": db_list_sessions(pid, owner_user_id=user.id)}
    except PermissionError:
        return JSONResponse({"error": "project not found"}, status_code=404)


@router.post("/control/projects/{pid}/sessions")
async def create_session(pid: int, req: SessionReq, user: Principal = Depends(get_current_user)):
    try:
        s = db_create_session(pid, req.title, owner_user_id=user.id)
    except PermissionError:
        return JSONResponse({"error": "project not found"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "session": s}


@router.patch("/control/sessions/{sid}")
async def update_session(sid: int, req: SessionReq, user: Principal = Depends(get_current_user)):
    try:
        db_update_session_title(sid, req.title, owner_user_id=user.id)
    except PermissionError:
        return JSONResponse({"error": "session not found"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True}


@router.delete("/control/sessions/{sid}")
async def delete_session(sid: int, user: Principal = Depends(get_current_user)):
    try:
        db_delete_session(sid, owner_user_id=user.id)
    except PermissionError:
        return JSONResponse({"error": "session not found"}, status_code=404)
    return {"ok": True}


@router.get("/control/sessions/{sid}/messages")
async def get_messages(sid: int, user: Principal = Depends(get_current_user)):
    try:
        return {"messages": db_load_messages(sid, owner_user_id=user.id)}
    except PermissionError:
        return JSONResponse({"error": "session not found"}, status_code=404)


@router.post("/control/sessions/{sid}/messages")
async def post_message(sid: int, req: MessageReq, user: Principal = Depends(get_current_user)):
    try:
        mid = db_append_message(sid, req.role, req.content, req.meta, owner_user_id=user.id)
    except PermissionError:
        return JSONResponse({"error": "session not found"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "id": mid}



