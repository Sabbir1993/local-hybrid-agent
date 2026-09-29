import json
from pathlib import Path
from typing import Optional
from fastapi import Depends
from pydantic import BaseModel
from core.audit import audit_log
from core.auth import Principal
from core.deps import get_current_user, require_permission
from core.config import atomic_write_json

from .base import router


SAMPLING_CONFIG_PATH = Path("config/sampling_config.json")


SAMPLING_DEFAULTS = {
    "sysprompt": "",
    "temp": 0.6,
    "topp": 0.95,
    "minp": 0.0,
    "rep": 1.0,
    "presence": 0.0,
    "topk": 20,
    "maxtok": -1,
}


class SamplingConfigRequest(BaseModel):
    sysprompt: Optional[str] = ""
    temp: Optional[float] = 0.6
    topp: Optional[float] = 0.95
    minp: Optional[float] = 0.0
    rep: Optional[float] = 1.0
    presence: Optional[float] = 0.0
    topk: Optional[int] = 20
    maxtok: Optional[int] = -1


@router.get("/control/sampling")
async def get_sampling_config(user: Principal = Depends(get_current_user)):
    if SAMPLING_CONFIG_PATH.exists():
        try:
            with open(SAMPLING_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return {**SAMPLING_DEFAULTS, **data}
        except Exception:
            pass
    return dict(SAMPLING_DEFAULTS)


@router.post("/control/sampling")
async def save_sampling_config(req: SamplingConfigRequest,
                               user: Principal = Depends(require_permission("settings.runtime.view"))):
    # one file shared by every user: only sampling-settings admins may change it
    cfg = {
        "sysprompt": str(req.sysprompt or ""),
        "temp": max(0.0, min(2.0, float(req.temp if req.temp is not None else 0.6))),
        "topp": max(0.0, min(1.0, float(req.topp if req.topp is not None else 0.95))),
        "minp": max(0.0, min(1.0, float(req.minp if req.minp is not None else 0.0))),
        "rep": max(0.0, min(3.0, float(req.rep if req.rep is not None else 1.0))),
        "presence": max(-2.0, min(2.0, float(req.presence if req.presence is not None else 0.0))),
        "topk": max(0, min(1000, int(req.topk if req.topk is not None else 20))),
        "maxtok": int(req.maxtok if req.maxtok is not None else -1),
    }
    SAMPLING_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(SAMPLING_CONFIG_PATH, cfg)
    audit_log(user, "sampling.update", f"Updated sampling config: temp={cfg['temp']}, topp={cfg['topp']}")
    return {"ok": True, "config": cfg}
