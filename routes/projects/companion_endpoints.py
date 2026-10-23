"""Companion status, filesystem browse/mkdir, and device management endpoints."""

import asyncio
import os
from typing import Optional

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse

from core import companion_bridge
from core.audit import audit_log
from core.auth import Principal
from core.db import db_list_user_devices, db_rename_device
from core import admin_terminal
from core.deps import get_current_user, require_permission, require_verified
from core.request_context import normalize_device_id, set_current_device
from core.shell_tools import always_refused_reason
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
async def companion_shell(request: Request,
                          req: ShellExecReq,
                          user: Principal = Depends(require_permission("chat.use"))):
    """One command typed into the dock terminal, run on the USER's machine
    through their companion. This is not the agent loop: the person typed it
    just now, so it is user-initiated by definition - but it still goes
    through the always-refused list, is permission-gated, and is audited.
    It never executes on the server: no companion, no run (same rule as
    fs_browse, which used to be violated by a local fallback here)."""
    cmd = (req.command or "").strip()
    if not cmd:
        return JSONResponse({"ok": False, "error": "Command cannot be empty"}, status_code=400)

    violation = always_refused_reason(cmd)
    if violation:
        audit_log(user, action="companion.shell", result="deny",
                  detail={"reason": violation}, ip=request.client.host if request.client else None)
        return JSONResponse({"ok": False, "error": violation}, status_code=400)

    target_cwd = (req.cwd or "").strip()
    if not target_cwd:
        try:
            from core.agent_tools.workspace import active_workspace
            target_cwd = str(active_workspace())
        except Exception:
            target_cwd = os.getcwd()

    timeout_s = min(max(int(req.timeout or 60), 1), 300)

    sh_type = (req.shell or "powershell").lower().strip()

    if req.elevated:
        # administrator rights are never implied: the person must have started an administrator session on their own PC,
        # still hold the permission, and that session must not have gone idle
        from core.auth import user_has_permission
        if not user_has_permission(user, "terminal.admin"):
            audit_log(user, action="companion.shell.admin", permission_key="terminal.admin", result="deny")
            return JSONResponse({"ok": False, "error": "missing permission: terminal.admin"}, status_code=403)
        if not admin_terminal.active(user.id):
            return JSONResponse({"ok": False, "admin_required": True,
                                 "error": "The administrator terminal is off (or timed out). Switch to System and confirm again."}, status_code=403)
        if sh_type not in ("powershell", "pwsh", "cmd"):
            return JSONResponse({"ok": False, "error": "Administrator mode supports PowerShell and CMD."}, status_code=400)

    if not companion_bridge.is_connected(user.id):
        return JSONResponse({
            "ok": False,
            "error": "The terminal runs commands on your own machine and requires the "
                     "companion app to be connected. Start the companion on this computer, "
                     "then try again.",
            "is_remote": True,
        }, status_code=400)

    # Audit records the action and the command's first word, never the full
    # text (a command line can contain arguments that are secrets in themselves).
    first_word = cmd.split()[0][:40].lower() if cmd.split() else ""
    audit_log(user, action="companion.shell.admin" if req.elevated else "companion.shell", result="allow",
              detail={"shell": sh_type, "command_name": first_word, "cwd": target_cwd, "elevated": bool(req.elevated)},
              ip=request.client.host if request.client else None)
    try:
        if req.elevated:
            admin_terminal.touch(user.id)
        data = await companion_bridge.call(
            user.id,
            "shell.admin_run" if req.elevated else "shell.run",
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
            "elevated": bool(req.elevated),
        }
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"companion: {e}"}, status_code=502)



# ---------------------------------------------------------------------------
# Administrator ("System") terminal. Opt-in, per session, confirmed on the user's own PC (a native window from the companion,
# then Windows UAC). Never stored, ends by itself when idle, and only commands the person types run elevated.
# ---------------------------------------------------------------------------

@router.get("/control/companion/admin-terminal")
async def admin_terminal_status(user: Principal = Depends(get_current_user)):
    from core.auth import user_has_permission
    return {"allowed": user_has_permission(user, "terminal.admin"), "active": admin_terminal.active(user.id),
            "expires_in": admin_terminal.remaining_seconds(user.id)}


@router.post("/control/companion/admin-terminal/start")
async def admin_terminal_start(request: Request, user: Principal = Depends(require_verified("terminal.admin"))):
    ip = request.client.host if request.client else None
    if not companion_bridge.is_connected(user.id):
        return JSONResponse({"ok": False, "error": "The administrator terminal needs the SSL Local Agent app running on your PC."},
                            status_code=400)
    try:
        # the companion shows its own confirmation window and then Windows asks for elevation (UAC): both can be declined
        data = await companion_bridge.call(user.id, "shell.admin_start", {"idle_min": admin_terminal.idle_seconds() / 60.0}, timeout=180)
    except Exception as e:
        audit_log(user, action="companion.admin_terminal.start", result="deny", detail={"reason": str(e)[:120]}, ip=ip)
        return JSONResponse({"ok": False, "error": f"{e}"}, status_code=502)
    admin_terminal.start(user.id)
    audit_log(user, action="companion.admin_terminal.start", result="allow", detail={"idle_min": admin_terminal.idle_seconds() / 60.0}, ip=ip)
    return {"ok": True, "active": True, "expires_in": admin_terminal.remaining_seconds(user.id), "user": (data or {}).get("user")}


@router.post("/control/companion/admin-terminal/stop")
async def admin_terminal_stop(request: Request, user: Principal = Depends(get_current_user)):
    was = admin_terminal.end(user.id)
    try:
        if companion_bridge.is_connected(user.id):
            await companion_bridge.call(user.id, "shell.admin_stop", {}, timeout=15)
    except Exception:
        pass                     # the companion's helper also ends by itself
    if was:
        audit_log(user, action="companion.admin_terminal.stop", result="allow", ip=request.client.host if request.client else None)
    return {"ok": True, "active": False}
