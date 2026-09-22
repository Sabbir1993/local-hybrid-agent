"""
routes/input_guard.py - Admin CRUD for the input sanitizer rules
(config/app.json "input_guard" block; engine in core/input_guard.py).
"""

import json
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.config import CONFIG_FILE
from core.small_model import APP_CONFIG
from core.auth import Principal
from core.deps import require_permission
from core.audit import audit_log
from core import input_guard

router = APIRouter(tags=["input_guard"])


class InputGuardSaveReq(BaseModel):
    enabled: bool = True
    rules: list = []


def _public(cfg_key: str) -> dict:
    cfg = dict(APP_CONFIG.get(cfg_key) or {}) if isinstance(APP_CONFIG.get(cfg_key), dict) else {}
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "rules": cfg.get("rules") or [],
    }


def _save(req: InputGuardSaveReq, cfg_key: str, user: Principal):
    clean, problems = input_guard.validate_rules(req.rules)
    block = {"enabled": bool(req.enabled), "rules": clean}

    # Persist to config/app.json (same pattern as capabilities/shell settings).
    cfg_path = CONFIG_FILE
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg[cfg_key] = block
        cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception as e:
        return JSONResponse({"error": f"config/app.json write failed: {e}"}, status_code=500)
    # Live update (hot reload for the running process).
    APP_CONFIG[cfg_key] = block
    input_guard._compile_cache.clear()
    input_guard._cache_ts = 0.0

    audit_log(user, action="input_guard.rules", resource=cfg_key,
              detail={"enabled": block["enabled"], "rules": len(clean),
                      "problems": problems}, result="allow")
    out = _public(cfg_key)
    if problems:
        out["problems"] = problems
    return out


@router.get("/control/input_guard")
async def get_input_guard(user: Principal = Depends(require_permission("settings.input_guard"))):
    return _public("input_guard")


@router.put("/control/input_guard")
async def save_input_guard(req: InputGuardSaveReq,
                           user: Principal = Depends(require_permission("settings.input_guard"))):
    return _save(req, "input_guard", user)


@router.get("/control/output_guard")
async def get_output_guard(user: Principal = Depends(require_permission("settings.input_guard"))):
    return _public("output_guard")


@router.put("/control/output_guard")
async def save_output_guard(req: InputGuardSaveReq,
                            user: Principal = Depends(require_permission("settings.input_guard"))):
    return _save(req, "output_guard", user)
