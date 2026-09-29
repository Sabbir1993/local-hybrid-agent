"""Project CRUD and activation endpoints."""

from typing import Optional

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse

from core.agent_tools import get_active_project, set_active_project, workspace_label
from core.auth import Principal
from core.db import (
    db_create_project,
    db_delete_project,
    db_list_projects,
    db_update_project_workspace,
)
from core.deps import get_current_user
from core.request_context import normalize_device_id, set_current_device
from .models import ProjectReq, UpdateWorkspaceReq

router = APIRouter()


@router.get("/control/projects")
async def list_projects(user: Principal = Depends(get_current_user),
                        device: Optional[str] = "current",
                        device_id: Optional[str] = None,
                        x_device_id: Optional[str] = Header(None, alias="X-Device-Id"),
                        x_device_name: Optional[str] = Header(None, alias="X-Device-Name")):
    cur_dev_id = normalize_device_id(device_id or x_device_id) or "default"
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
    dev_id = normalize_device_id(req.device_id or x_device_id) or "default"
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
    dev_id = normalize_device_id(req.device_id or x_device_id) or "default"
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
    dev_id = normalize_device_id(x_device_id) or "default"
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
    dev_id = normalize_device_id(x_device_id) or "default"
    set_current_device(dev_id, x_device_name)
    if pid == 0:
        set_active_project(None, user.id, dev_id)
        return {"ok": True, "active": None, "workspace": workspace_label()}
    for p in db_list_projects(owner_user_id=user.id):
        if p["id"] == pid:
            set_active_project(p["name"], user.id, dev_id)
            return {"ok": True, "active": p["name"], "project": p, "workspace": workspace_label()}
    return JSONResponse({"error": "project not found"}, status_code=404)
