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
    "rlast": 64,
    "freq": 0.0,
    "seed": -1,
    "drym": 0.0,
    "dryb": 1.75,
    "dryl": 2,
    "dryn": 4096,
    "dynr": 0.0,
    "dyne": 1.0,
}

# key -> (kind, min, max); shared by the POST clamp and the chat/agent request path
SAMPLING_EXTRA_LIMITS = {
    "rlast": (int, -1, 8192),
    "freq": (float, -2.0, 2.0),
    "seed": (int, -1, 2147483647),
    "drym": (float, 0.0, 5.0),
    "dryb": (float, 1.0, 4.0),
    "dryl": (int, 0, 64),
    "dryn": (int, -1, 32768),
    "dynr": (float, 0.0, 2.0),
    "dyne": (float, 0.1, 5.0),
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
    rlast: Optional[int] = 64
    freq: Optional[float] = 0.0
    seed: Optional[int] = -1
    drym: Optional[float] = 0.0
    dryb: Optional[float] = 1.75
    dryl: Optional[int] = 2
    dryn: Optional[int] = 4096
    dynr: Optional[float] = 0.0
    dyne: Optional[float] = 1.0


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
    for key, (kind, lo, hi) in SAMPLING_EXTRA_LIMITS.items():
        val = getattr(req, key)
        val = SAMPLING_DEFAULTS[key] if val is None else kind(val)
        cfg[key] = max(lo, min(hi, val))
    SAMPLING_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(SAMPLING_CONFIG_PATH, cfg)
    audit_log(user, "sampling.update", f"Updated sampling config: temp={cfg['temp']}, topp={cfg['topp']}")
    return {"ok": True, "config": cfg}
