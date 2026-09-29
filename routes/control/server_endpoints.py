import asyncio
import json
import time
from pathlib import Path
from typing import Optional
from fastapi import Depends
from fastapi.responses import JSONResponse
from core.audit import audit_log
from core.auth import Principal, user_has_permission
from core.deps import get_current_user, require_permission
from core.profiles import build_dynamic_profile, in_models_dir
from core.state import state
from core import cloud
from core import vram
from .. import common

from .base import router
from .models import KeepaliveRequest, SwitchRequest


@router.post("/control/stop")
async def stop_server(user: Principal = Depends(require_permission("model.local.load"))):
    was = state.profile.get("name") if state.profile else None
    await state.stop()
    state.profile = None
    audit_log(user, action="model.stop", resource=was, result="allow")
    return {"ok": True, "stopped": True}


@router.get("/control/preflight")
async def preflight(target: Optional[str] = None,
                    user: Principal = Depends(require_permission("model.local.load"))):
    """VRAM fit projection for a model/profile without loading it.
    ?target=<gguf or profile json path> - defaults to the currently selected profile."""
    try:
        if target:
            p = Path(target)
            # only model files under the models directory -- never an arbitrary server path
            if not in_models_dir(p):
                return JSONResponse({"error": "target must be inside the models directory"}, status_code=400)
            if p.suffix == ".json" and p.exists():
                profile = json.loads(p.read_text())
            elif p.exists():
                profile = build_dynamic_profile(p)
            else:
                return JSONResponse({"error": f"target not found: {target}"}, status_code=404)
        elif state.profile is not None:
            profile = state.profile
        else:
            return JSONResponse({"error": "no model/profile selected"}, status_code=400)
        loop = asyncio.get_event_loop()
        plan = await loop.run_in_executor(None, vram.plan_launch, profile)
        return plan
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/control/vram")
async def vram_devices(user: Principal = Depends(require_permission("settings.runtime.view"))):
    """Raw per-Vulkan-device free/total VRAM (VK_EXT memory budget)."""
    loop = asyncio.get_event_loop()
    devs = await loop.run_in_executor(None, vram.query_devices, None, True)
    return {"devices": [
        {"index": d["index"], "name": d["name"],
         "total_gb": round(d["total_b"] / (1024 ** 3), 2),
         "free_gb": round(d["free_b"] / (1024 ** 3), 2),
         "used_gb": round(d["used_b"] / (1024 ** 3), 2)}
        for d in devs]}


@router.post("/control/start")
async def start_server(user: Principal = Depends(require_permission("model.local.load"))):
    if state.profile is None and state.profile_path is None:
        return JSONResponse({"error": "No model or profile selected. Select a model/profile first."}, status_code=400)
    try:
        await state.start()
    except Exception as e:
        audit_log(user, action="model.start", result="error", detail={"error": str(e)})
        return JSONResponse({"error": str(e)}, status_code=500)
    audit_log(user, action="model.start", resource=state.profile.get("name") if state.profile else None, result="allow")
    return {"ok": True, "pid": state.process.pid if state.process else None}


@router.post("/control/restart")
async def restart_server(user: Principal = Depends(require_permission("model.local.load"))):
    try:
        await state.start()
    except Exception as e:
        audit_log(user, action="model.restart", result="error", detail={"error": str(e)})
        return JSONResponse({"error": str(e)}, status_code=500)
    audit_log(user, action="model.restart", resource=state.profile.get("name") if state.profile else None, result="allow")
    return {"ok": True, "pid": state.process.pid if state.process else None}


@router.post("/control/keepalive")
async def set_keepalive(req: KeepaliveRequest, user: Principal = Depends(require_permission("settings.runtime.view"))):
    state.keepalive_enabled = req.enabled
    state.last_activity = time.time()
    print(f"[server_manager] keepalive {'enabled' if req.enabled else 'disabled'}")
    return {"ok": True, "keepalive": state.keepalive_enabled}


@router.post("/control/switch")
async def switch(req: SwitchRequest, user: Principal = Depends(get_current_user)):
    # global common.curStatus_model_hint
    target = req.target or req.profile
    if not target:
        return JSONResponse({"error": "No profile or model target specified"}, status_code=400)

    # Cloud model: bind the MAIN lane to it and never spawn llama-server. The
    # local model, if any, is left untouched (selection is who answers you).
    # Cloud provider/model choice is fully user-managed -- no permission gate.
    if str(target).startswith("cloud:"):
        cm = cloud.get_cloud(target, user.id)
        if cm is None:
            return JSONResponse({"error": f"cloud model not configured: {target[6:]}"}, status_code=404)
        cloud.set_lanes(user.id, {"main": cm.key})
        common.curStatus_model_hint = target
        print(f"[server_manager] main lane -> cloud {cm.key} ({cm.provider_name}); "
              f"local llama-server not started")
        audit_log(user, action="model.load", resource=cm.key, detail={"cloud": True}, result="allow")
        return {"ok": True, "cloud": True, "model": cm.key, "display": cm.display,
                "provider": cm.provider_name, "endpoint": cm.endpoint()}

    # Local GGUF: launching a process on the shared GPU rig stays privileged.
    if not user_has_permission(user, "model.local.load"):
        audit_log(user, action="model.local.load", permission_key="model.local.load", result="deny")
        return JSONResponse({"error": "missing permission: model.local.load"}, status_code=403)

    path = Path(target)
    # only GGUFs under the models directory: this path is handed to llama-server
    if not in_models_dir(path) or path.suffix.lower() != ".gguf":
        return JSONResponse({"error": "target must be a .gguf inside the models directory"}, status_code=400)
    if not path.exists():
        return JSONResponse({"error": f"Target file not found: {target}"}, status_code=404)

    # Selecting a local model releases the main lane from the cloud (if bound)
    if cloud.cloud_bindings(user.id).get("main"):
        cloud.set_lanes(user.id, {"main": None})
        print("[server_manager] main lane -> local (cloud binding cleared)")
    try:
        await state.load_profile(path)
    except Exception as e:
        audit_log(user, action="model.load", resource=str(path), result="error", detail={"error": str(e)})
        return JSONResponse({"error": str(e)}, status_code=500)
    common.curStatus_model_hint = state.profile.get("model_path") if state.profile else None
    audit_log(user, action="model.load", resource=state.profile.get("name", path.stem), result="allow")
    return {"ok": True, "profile": state.profile.get("name", path.stem)}
