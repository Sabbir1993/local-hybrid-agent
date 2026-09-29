"""
routes/lanes/endpoints.py - General lane CRUD endpoints + test + media-settings + verification.

  GET    /control/lanes                   everything the page needs (plain-language)
  POST   /control/lanes                   add/edit a model (local: admin; cloud: own)
  DELETE /control/lanes?name=&move_to=    remove a model, moving its jobs elsewhere
  GET    /control/lanes/files             model files in Models/orchestrator (admin)
  POST   /control/lanes/roles             map jobs to models (as_default: admin)
  POST   /control/lanes/{name}/test       one tiny request, plain-language result
  POST   /control/media-settings          cloud speech switch + daily limits (admin)
  POST   /control/verification            answer-check settings (per user)
"""

import asyncio
import time
from typing import Optional

from fastapi import APIRouter, Depends

from core import cloud, lanes, verifier
from core.audit import audit_log
from core.auth import Principal, user_has_permission
from core.deps import get_current_user

from .helpers import _LOCAL_PERM, _FILES_CACHE, _err, _helper_files, _view
from .models import LaneReq, MediaSettingsReq, RolesReq, VerifyReq
from .test_helpers import _plain_error, _test_media

router = APIRouter()


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


@router.post("/control/lanes/{name}/test")
async def lanes_test(name: str, full: bool = False, user: Principal = Depends(get_current_user)):
    reg = lanes.registry(user.id)
    d = reg.get(name)
    if d is None:
        return _err(f"there is no model called '{name}'", 404)
    if d["kind"] in lanes.MEDIA_KINDS:
        return await _test_media(name, d, user, full)
    if d["cloud_key"]:
        cm = cloud.cloud_lane(name, user.id)
        r = await cloud.probe(cm, "Reply with the single word: ready")
        if r.get("ok"):
            return {"ok": True, "ms": r.get("ms"), "sample": r.get("sample"), "where": "cloud"}
        return {"ok": False, "error": _plain_error(Exception(r.get("error") or "")), "where": "cloud"}
    if d["local"] and name != "main" and not user_has_permission(user, "model.local.load"):
        return _err("You don't have permission to start local models.", 403)
    t = lanes.Target(name)
    t0 = time.time()
    try:
        c = await t.client()
        if d["kind"] == "embed":
            r = await c.post("/v1/embeddings", json={"input": ["ready"]}, timeout=60)
            r.raise_for_status()
            dim = len(((r.json().get("data") or [{}])[0]).get("embedding") or [])
            sample = f"embedding size {dim}"
        else:
            r = await c.post("/v1/chat/completions", json={
                "messages": [{"role": "user", "content": "Reply with the single word: ready"}],
                "max_tokens": 16, "temperature": 0}, timeout=60)
            r.raise_for_status()
            sample = lanes.message_text(r.json()).strip()[:200]
    except Exception as e:
        return {"ok": False, "error": _plain_error(e), "where": "local"}
    return {"ok": True, "ms": int((time.time() - t0) * 1000), "sample": sample, "where": "local"}


@router.post("/control/media-settings")
async def media_settings_save(req: MediaSettingsReq, user: Principal = Depends(get_current_user)):
    """Admin: the cloud speech switch + daily cloud generation limits (app.json "media")."""
    if not user_has_permission(user, _LOCAL_PERM):
        return _err("Only an admin can change these settings.", 403)
    from core.config import update_app_config
    from core.small_model import APP_CONFIG
    upd = req.model_dump(exclude_none=True)

    def _apply(m: dict) -> None:
        if "allow_cloud_audio" in upd:
            m["allow_cloud_audio"] = upd["allow_cloud_audio"]
        for k in ("image_per_day", "video_per_day"):
            if k in upd:
                m.setdefault("limits", {})[k] = upd[k]
    update_app_config(lambda cfg: _apply(cfg.setdefault("media", {})))
    _apply(APP_CONFIG.setdefault("media", {}))
    audit_log(user, action="media.settings", detail=upd, result="allow")
    return {"ok": True, **_view(user)}


@router.post("/control/verification")
async def verification_save(req: VerifyReq, user: Principal = Depends(get_current_user)):
    cur = dict(cloud.verification(user.id))
    upd = req.model_dump(exclude_none=True)
    if "mode" in upd and upd["mode"] not in verifier.MODES:
        return _err("mode must be off, badge or gate")
    if "apply_to" in upd and upd["apply_to"] not in verifier.APPLY_TO:
        return _err("apply_to must be both, chat or agent")
    cur.update(upd)
    cloud.write_user_section(user.id, "verification", cur)
    audit_log(user, action="verification.save", detail=upd, result="allow")
    return {"ok": True, **_view(user)}
