import json
from pathlib import Path
from typing import Optional
from fastapi import Depends
from pydantic import BaseModel
from core.audit import audit_log
from core.auth import Principal
from core.deps import get_current_user, require_permission
from core.config import atomic_write_json
from core.subagent.constants import SUBAGENT_CONFIG_PATH, SUBAGENT_DEFAULTS, load_subagent_config
from .base import router


class SubagentConfigRequest(BaseModel):
    subagent_default_steps: Optional[int] = 40
    subagent_max_steps: Optional[int] = 80
    subagent_timeout_s: Optional[int] = 3600
    subagent_max_parallel: Optional[int] = 4


@router.get("/control/subagents")
async def get_subagents_config(user: Principal = Depends(get_current_user)):
    return load_subagent_config()


@router.post("/control/subagents")
async def save_subagents_config(req: SubagentConfigRequest,
                                user: Principal = Depends(require_permission("settings.orchestration.configure"))):
    cfg = {
        "subagent_default_steps": max(1, min(100, int(req.subagent_default_steps if req.subagent_default_steps is not None else 40))),
        "subagent_max_steps": max(5, min(150, int(req.subagent_max_steps if req.subagent_max_steps is not None else 80))),
        "subagent_timeout_s": max(30, min(7200, int(req.subagent_timeout_s if req.subagent_timeout_s is not None else 3600))),
        "subagent_max_parallel": max(1, min(16, int(req.subagent_max_parallel if req.subagent_max_parallel is not None else 4))),
    }
    # Ensure default does not exceed max
    if cfg["subagent_default_steps"] > cfg["subagent_max_steps"]:
        cfg["subagent_default_steps"] = cfg["subagent_max_steps"]

    SUBAGENT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(SUBAGENT_CONFIG_PATH, cfg)

    # Update in-memory APP_CONFIG as well
    try:
        from core.small_model import APP_CONFIG
        APP_CONFIG["subagents"] = dict(cfg)
    except Exception:
        pass

    audit_log(user, "subagents.update", f"Updated subagent limits: default_steps={cfg['subagent_default_steps']}, max_steps={cfg['subagent_max_steps']}, timeout={cfg['subagent_timeout_s']}s, parallel={cfg['subagent_max_parallel']}")
    return {"ok": True, "config": cfg}
