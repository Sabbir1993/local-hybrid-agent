from typing import Optional
from fastapi import Depends
from fastapi.responses import JSONResponse
from core.auth import Principal
from core.deps import get_current_user, require_permission
from core import reasoning
from core.profiles import save_model_config, _model_key
from core.state import state
from core import cloud
from .. import common

from .base import router
from .config_helpers import (
    _apply_config_update,
    _config_for_profile,
    _reasoning_caps,
    _standalone_profile,
)
from .models import ConfigRequest


@router.get("/control/config")
async def get_config(model: Optional[str] = None, user: Principal = Depends(get_current_user)):
    # Cloud model selected in the dropdown: llama-server launch params don't
    # apply, so report a cloud-shaped config (the drawer disables the fields).
    if model and str(model).startswith("cloud:"):
        cm = cloud.get_cloud(model, user.id)
        if cm:
            return {"cloud": True, "provider": cm.provider_name, "model": cm.model_id,
                    "display": cm.display, "ctx": cm.ctx, "context_size": cm.ctx,
                    "endpoint": cm.endpoint(),
                    "tools_available": True,
                    **_reasoning_caps(reasoning.cloud_mode(cm)),
                    "vision_capable": any(k in (cm.model_id or "").lower() for k in ["vision", "4o", "gemini", "claude-3", "vl"])}
    # No ?model= given: if the main lane itself is cloud-bound, report that
    if not model:
        cm_bound = cloud.cloud_lane("main", user.id)
        if cm_bound:
            return {"cloud": True, "provider": cm_bound.provider_name, "model": cm_bound.model_id,
                    "display": cm_bound.display, "ctx": cm_bound.ctx,
                    "context_size": cm_bound.ctx, "endpoint": cm_bound.endpoint(),
                    "tools_available": True,
                    **_reasoning_caps(reasoning.cloud_mode(cm_bound)),
                    "vision_capable": any(k in (cm_bound.model_id or "").lower() for k in ["vision", "4o", "gemini", "claude-3", "vl"])}
    # The ?model= target wins when it names a different model than the one
    # loaded — the drawer edits the dropdown-selected model, not what's in VRAM.
    target = model or common.get_model_hint(user.id)
    if state.profile is not None:
        loaded_key = _model_key(state.profile.get("model_path") or "")
        if not model or _model_key(model) == loaded_key:
            return _config_for_profile(state.profile)
    if target:
        prof = _standalone_profile(target)
        if prof:
            return _config_for_profile(prof)
    if state.profile is not None:
        return _config_for_profile(state.profile)
    return JSONResponse({"error": "no profile loaded"}, status_code=400)


@router.post("/control/config")
async def set_config(req: ConfigRequest, model: Optional[str] = None,
                      user: Principal = Depends(require_permission("model.local.configure"))):
    # validate + persist against the live profile when it matches the request
    # target (or no target); otherwise against a temp profile built from the
    # selected model's saved config
    if state.profile is not None:
        loaded_key = _model_key(state.profile.get("model_path") or "")
        if not model or _model_key(model) == loaded_key:
            prof = state.profile
        else:
            prof = _standalone_profile(model)
            if prof is None:
                return JSONResponse({"error": f"model file not found: {model}"}, status_code=404)
    else:
        if not target:
            return JSONResponse({"error": "no profile loaded"}, status_code=400)
        prof = _standalone_profile(target)
        if prof is None:
            return JSONResponse({"error": f"model file not found: {target}"}, status_code=404)
    errors = []
    for key, value in req.updates.items():
        err = _apply_config_update(prof, key, value)
        if err:
            errors.append(err)
    if errors:
        return JSONResponse({"error": "; ".join(errors)}, status_code=400)
    m_id = prof.get("model_path") or prof.get("name")
    if req.persist and m_id:
        save_model_config(m_id, prof)
        common.set_model_hint(user.id, m_id)
    was_running = state.process is not None and state.process.poll() is None
    if req.restart and was_running and state.profile is not None:
        try:
            target = state.profile_path or state.profile
            await state.load_profile(target)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    return {"ok": True, "restarted": req.restart and was_running,
            "config": _config_for_profile(prof)}
