"""Companion status, filesystem browse/mkdir, and device management endpoints."""

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse

from core import companion_bridge
from core.auth import Principal
from core.db import db_list_user_devices, db_rename_device
from core.deps import get_current_user
from core.request_context import normalize_device_id, set_current_device
from .helpers import _ask_directory_native
from .models import BrowseFolderReq, MkdirReq, RenameDeviceReq

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
