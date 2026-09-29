from fastapi import Depends
from core import cloud, verifier
from core.audit import audit_log
from core.auth import Principal, user_has_permission
from core.deps import get_current_user

from .helpers import _LOCAL_PERM, _err, _view, router
from .models import MediaSettingsReq, VerifyReq


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
