import json
from fastapi import Depends
from fastapi.responses import JSONResponse
from core.audit import audit_log
from core.auth import Principal
from core.config import CONFIG_FILE, write_app_config
from core.deps import require_permission
from core.small_model import APP_CONFIG
from ..models import (
    AGENT_STEPS_MAX,
    AGENT_STEPS_MIN,
    AGENT_TIMEOUT_MAX_S,
    AGENT_TIMEOUT_MIN_S,
    AgentSettingsReq,
    ShellSettingsReq,
)

from .base import router


@router.post("/control/shell_settings")
async def shell_settings(req: ShellSettingsReq,
                          user: Principal = Depends(require_permission("settings.shell.configure"))):
    """Edit the GLOBAL shell permission settings; persists to config/app.json.

    Admin-only: this is the org-wide allowlist. Per-user additions go through
    the permission modal ("Always allow for me") -> core/auth_db.py
    user_allow_patterns, not this endpoint.
    """
    import json as _json
    cfg_path = CONFIG_FILE
    try:
        cfg = _json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception as e:
        return JSONResponse({"error": f"config/app.json unreadable: {e}"}, status_code=500)
    shell = cfg.setdefault("capabilities", {}).setdefault("shell", {})
    if req.ask_first is not None:
        shell["ask_first"] = bool(req.ask_first)
    if req.allow_patterns is not None:
        shell["allow_patterns"] = [str(p).strip() for p in req.allow_patterns if str(p).strip()]
    if req.timeout_s is not None:
        shell["timeout_s"] = max(5, min(600, int(req.timeout_s)))
    try:
        write_app_config(cfg, CONFIG_FILE)
    except Exception as e:
        return JSONResponse({"error": f"config/app.json write failed: {e}"}, status_code=500)
    # live update
    APP_CONFIG["capabilities"]["shell"] = shell
    audit_log(user, action="settings.shell.configure", resource="capabilities.shell",
              permission_key="settings.shell.configure",
              detail={k: v for k, v in req.model_dump().items() if v is not None})
    return {"ok": True, "shell": shell}


@router.post("/control/agent_settings")
async def agent_settings(req: AgentSettingsReq,
                         user: Principal = Depends(require_permission("settings.orchestration.configure"))):
    """Agent run limits: the step cap (agent.max_steps) and the wall-clock budget
    (agent.run_timeout_s). Both persist to config/app.json.

    The step cap alone is not a safety mechanism - it kills slow but legitimate
    work while doing nothing to stop a thrashing model. run_timeout_s is the
    bound that actually protects the GPU. 0 disables it."""
    if req.max_steps is None and req.run_timeout_s is None:
        return JSONResponse({"error": "max_steps or run_timeout_s required"}, status_code=400)
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        return JSONResponse({"error": f"config/app.json unreadable: {e}"}, status_code=500)
    agent_cfg = cfg.setdefault("agent", {})
    old_steps = agent_cfg.get("max_steps")
    old_timeout = agent_cfg.get("run_timeout_s")
    if req.max_steps is not None:
        agent_cfg["max_steps"] = max(AGENT_STEPS_MIN, min(AGENT_STEPS_MAX, int(req.max_steps)))
    if req.run_timeout_s is not None:
        agent_cfg["run_timeout_s"] = max(AGENT_TIMEOUT_MIN_S,
                                          min(AGENT_TIMEOUT_MAX_S, int(req.run_timeout_s)))
    try:
        write_app_config(cfg, CONFIG_FILE)
    except Exception as e:
        return JSONResponse({"error": f"config/app.json write failed: {e}"}, status_code=500)
    APP_CONFIG.setdefault("agent", {}).update(
        {k: v for k, v in agent_cfg.items() if k in ("max_steps", "run_timeout_s")})
    audit_log(user, action="agent.settings", resource="agent.limits",
              permission_key="settings.orchestration.configure",
              detail={"max_steps": [old_steps, agent_cfg.get("max_steps")],
                      "run_timeout_s": [old_timeout, agent_cfg.get("run_timeout_s")]})
    return {"ok": True, "max_steps": agent_cfg.get("max_steps"),
            "run_timeout_s": agent_cfg.get("run_timeout_s")}
