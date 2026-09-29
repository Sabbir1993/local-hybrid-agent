"""
routes/lanes/helpers.py - Shared helpers: _view(), _FILES_CACHE, _helper_files(), _err().
"""

import time

from fastapi.responses import JSONResponse

from core import cloud, lanes, verifier
from core.auth import Principal, user_has_permission
from core.config import ACTIVE_RUNTIME

_LOCAL_PERM = "model.local.configure"


def _view(user: Principal) -> dict:
    out = lanes.public_view(user.id, is_admin=user_has_permission(user, _LOCAL_PERM))
    out["cloud_models"] = [{"key": cm.key, "display": cm.display, "provider": cm.provider_name}
                           for cm in cloud.cloud_models(user.id)]
    out["gpus"] = list(ACTIVE_RUNTIME.get("gpu_devices") or [])
    out["suggested_port"] = lanes.suggest_port()
    out["verification"] = verifier.settings(user.id)
    out["fallback_local"] = bool(cloud.cloud_bindings(user.id).get("fallback_local", True))
    return out


_FILES_CACHE = {"t": 0.0, "items": None}


def _helper_files() -> dict:
    """Model files in Models/orchestrator for the add/edit wizard (60 s cache)."""
    from core import profiles
    if _FILES_CACHE["items"] is not None and time.time() - _FILES_CACHE["t"] < 60:
        return _FILES_CACHE["items"]
    items = profiles.discover_helper_files()
    _FILES_CACHE.update(t=time.time(), items=items)
    return items


def _err(msg: str, code: int = 400):
    return JSONResponse({"error": msg}, status_code=code)
