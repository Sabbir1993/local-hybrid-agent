import asyncio
from typing import Optional
from fastapi import Depends
from core import lanes
from core.audit import audit_log
from core.auth import Principal, user_has_permission
from core.deps import get_current_user

from .helpers import _FILES_CACHE, _LOCAL_PERM, _err, _helper_files, _view, router
from .models import LaneReq, RolesReq


@router.get("/control/lanes")
async def lanes_get(user: Principal = Depends(get_current_user)):
    return _view(user)


@router.get("/control/lanes/files")
async def lanes_files(fresh: bool = False, user: Principal = Depends(get_current_user)):
    """Helper / whisper / image / video model files (admins only: they pick local models)."""
    if not user_has_permission(user, _LOCAL_PERM):
        return _err("Only an admin can add models on this PC.", 403)
    if fresh:
        _FILES_CACHE["items"] = None
    return await asyncio.to_thread(_helper_files)


@router.post("/control/lanes")
async def lanes_save(req: LaneReq, user: Principal = Depends(get_current_user)):
    name = req.name.strip().lower()
    data = req.model_dump(exclude_unset=True)
    reg = lanes.registry(user.id)
    is_local = req.backend == "local" or (name in reg and reg[name]["local"])
    try:
        if is_local:
            if not user_has_permission(user, _LOCAL_PERM):
                audit_log(user, action="lanes.local.save", resource=name, result="deny")
                return _err("Only an admin can add or change models that run on this PC. "
                            "You can add your own cloud models.", 403)
            lanes.save_local_lane(name, data)
        else:
            lanes.save_user_lane(user.id, name, data)
        if req.jobs:
            lanes.set_role_map(user.id, {j: name for j in req.jobs})
    except ValueError as e:
        return _err(str(e))
    # never the auth header in the audit trail
    audit_log(user, action="lanes.save", resource=name,
              detail={"backend": "local" if is_local else "cloud", "kind": req.kind}, result="allow")
    return {"ok": True, **_view(user)}


@router.delete("/control/lanes")
async def lanes_delete(name: str, move_to: Optional[str] = None,
                       user: Principal = Depends(get_current_user)):
    reg = lanes.registry(user.id)
    d = reg.get(name)
    if d is None:
        return _err(f"there is no model called '{name}'", 404)
    if move_to and move_to not in reg:
        return _err(f"there is no model called '{move_to}'")
    try:
        if d["owner"] == "user":
            lanes.delete_user_lane(user.id, name, move_to)
        else:
            if not user_has_permission(user, _LOCAL_PERM):
                return _err("Only an admin can remove models that run on this PC.", 403)
            lanes.delete_local_lane(name, move_to)
    except ValueError as e:
        return _err(str(e))
    audit_log(user, action="lanes.delete", resource=name, detail={"move_to": move_to}, result="allow")
    return {"ok": True, **_view(user)}


@router.post("/control/lanes/roles")
async def lanes_roles(req: RolesReq, user: Principal = Depends(get_current_user)):
    if req.as_default and not user_has_permission(user, _LOCAL_PERM):
        return _err("Only an admin can change the default for everyone.", 403)
    try:
        lanes.set_role_map(user.id, req.map, as_default=req.as_default)
    except ValueError as e:
        return _err(str(e))
    audit_log(user, action="lanes.roles", detail={"map": req.map, "as_default": req.as_default},
              result="allow")
    return {"ok": True, **_view(user)}
