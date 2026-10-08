"""Companion status, filesystem browse/mkdir, and device management endpoints."""

import asyncio
import os
from typing import Optional

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse

from core import companion_bridge
from core.auth import Principal
from core.db import db_list_user_devices, db_rename_device
from core.deps import get_current_user
from core.request_context import normalize_device_id, set_current_device
from .helpers import _ask_directory_native
from .models import BrowseFolderReq, MkdirReq, RenameDeviceReq, ShellExecReq

router = APIRouter()


@router.get("/control/companion/status")
async def companion_status(user: Principal = Depends(get_current_user)):
    info = companion_bridge.connection_info(user.id)
    return {"connected": info is not None, **(info or {})}


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

    # No server-disk fallback: project folders live on users' machines, and a
    # "localhost" client may be anyone arriving through a local tunnel/proxy.
    return JSONResponse({
        "ok": False,
        "error": "In-app filesystem browsing requires the local companion app on your machine.",
        "is_remote": True,
    }, status_code=400)


@router.post("/control/fs/mkdir")
async def fs_mkdir(req: MkdirReq, user: Principal = Depends(get_current_user)):
    if companion_bridge.is_connected(user.id):
        try:
            data = await companion_bridge.call(user.id, "fs.mkdir", {"path": req.path, "name": req.name})
            return {"ok": True, **data}
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"companion: {e}"}, status_code=502)
    # never create folders on the server's own disk (see fs_browse)
    return JSONResponse({"ok": False, "error": "Creating folders requires the companion app on your machine.",
                         "is_remote": True}, status_code=400)


@router.post("/control/device/rename")
async def rename_device(req: RenameDeviceReq,
                        user: Principal = Depends(get_current_user),
                        x_device_id: Optional[str] = Header(None, alias="X-Device-Id")):
    new_name = req.new_name.strip()
    if not new_name:
        return JSONResponse({"error": "Device name cannot be empty"}, status_code=400)
    dev_id = normalize_device_id(req.device_id or x_device_id)
    db_rename_device(owner_user_id=user.id, new_name=new_name, device_id=dev_id, old_name=req.old_name)
    set_current_device(dev_id or "default", new_name)
    return {"ok": True, "device_name": new_name}


@router.get("/control/user/devices")
async def list_user_devices(user: Principal = Depends(get_current_user)):
    return {"ok": True, "devices": db_list_user_devices(user.id)}


@router.post("/control/companion/shell")
async def companion_shell(req: ShellExecReq, user: Principal = Depends(get_current_user)):
    cmd = (req.command or "").strip()
    if not cmd:
        return JSONResponse({"ok": False, "error": "Command cannot be empty"}, status_code=400)

    target_cwd = (req.cwd or "").strip()
    if not target_cwd:
        try:
            from core.agent_tools.workspace import active_workspace
            target_cwd = str(active_workspace())
        except Exception:
            target_cwd = os.getcwd()

    timeout_s = min(max(int(req.timeout or 60), 1), 300)

    sh_type = (req.shell or "powershell").lower().strip()

    # 1. Dispatch to paired companion if connected
    if companion_bridge.is_connected(user.id):
        try:
            data = await companion_bridge.call(
                user.id,
                "shell.run",
                {"command": cmd, "cwd": target_cwd, "timeout": timeout_s, "approved_in_app": True, "shell": sh_type},
                timeout=timeout_s + 10,
            )
            return {
                "ok": True,
                "exit_code": data.get("exit_code", 0),
                "stdout": data.get("stdout", ""),
                "stderr": data.get("stderr", ""),
                "cwd": target_cwd,
                "shell": sh_type,
                "source": "companion",
            }
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"companion: {e}"}, status_code=502)

    # 2. Local fallback if server/companion runs on localhost
    try:
        run_args = None
        if os.name == "nt":
            if sh_type == "cmd":
                run_args = ["cmd.exe", "/c", cmd]
            elif sh_type == "bash":
                run_args = ["bash.exe", "-c", cmd]
            elif sh_type == "pwsh":
                run_args = ["pwsh.exe", "-NoProfile", "-Command", cmd]
            else:  # default powershell
                run_args = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd]
        else:
            if sh_type in ("bash", "sh"):
                run_args = ["/bin/bash", "-c", cmd]
            else:
                run_args = ["/bin/sh", "-c", cmd]

        safe_cwd = target_cwd if os.path.exists(target_cwd) else os.getcwd()
        proc = await asyncio.create_subprocess_exec(
            *run_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=safe_cwd,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
            return {
                "ok": True,
                "exit_code": proc.returncode,
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
                "cwd": safe_cwd,
                "shell": sh_type,
                "source": "local",
            }
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return {"ok": False, "error": f"command timed out after {timeout_s}s", "exit_code": -1, "cwd": safe_cwd}
    except FileNotFoundError:
        return JSONResponse({
            "ok": False,
            "error": f"Shell interpreter '{sh_type}' was not found on your system (PATH). Try selecting PowerShell or CMD.",
            "exit_code": 127,
            "cwd": safe_cwd,
            "shell": sh_type
        }, status_code=200)
    except Exception as e:
        return JSONResponse({
            "ok": False,
            "error": f"Command execution failed: {e}",
            "exit_code": 1,
            "cwd": safe_cwd if 'safe_cwd' in locals() else target_cwd,
            "shell": sh_type
        }, status_code=200)

