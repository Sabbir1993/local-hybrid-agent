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
    AgentLimitsReq,
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


def _limits_view() -> dict:
    from core.agent_tools.limits import (AGENT_LIMITS, CONTEXT_LIMITS, MEMORY_LIMITS, agent_limit, context_limit,
                                         executor_effort_ceiling, memory_limit)
    from core.agent_loop import plan_guard
    agent_cfg = APP_CONFIG.get("agent") or {}
    mem_cfg = APP_CONFIG.get("memory") or {}
    return {
        "values": {
            **{k: agent_limit(k) for k in AGENT_LIMITS},
            "mirror_to_workspace": bool(agent_cfg.get("mirror_to_workspace", False)),
            "executor_max_effort": executor_effort_ceiling(),
            "plan_required": plan_guard.plan_mode_setting(agent_cfg),
            "compaction_threshold": context_limit("compaction_threshold"),
            "memory_enabled": bool(mem_cfg.get("enabled", True)),
            "memory_allow_cloud": bool(mem_cfg.get("allow_cloud", False)),
            "memory_file_max_bytes": memory_limit("file_max_bytes"),
            "memory_max_files": memory_limit("max_files"),
        },
        "ranges": {
            **{k: [lo, hi] for k, (_, lo, hi) in AGENT_LIMITS.items()},
            "compaction_threshold": [CONTEXT_LIMITS["compaction_threshold"][1], CONTEXT_LIMITS["compaction_threshold"][2]],
            "memory_file_max_bytes": [MEMORY_LIMITS["file_max_bytes"][1], MEMORY_LIMITS["file_max_bytes"][2]],
            "memory_max_files": [MEMORY_LIMITS["max_files"][1], MEMORY_LIMITS["max_files"][2]],
        },
        "defaults": {**{k: v[0] for k, v in AGENT_LIMITS.items()},
                     "compaction_threshold": CONTEXT_LIMITS["compaction_threshold"][0],
                     "memory_file_max_bytes": MEMORY_LIMITS["file_max_bytes"][0],
                     "memory_max_files": MEMORY_LIMITS["max_files"][0]},
    }


@router.get("/control/agent_limits")
async def agent_limits_get(user: Principal = Depends(require_permission("settings.orchestration.configure"))):
    """Output caps per lane, file-tool limits, compaction threshold and memory limits."""
    return _limits_view()


@router.post("/control/agent_limits")
async def agent_limits_set(req: AgentLimitsReq,
                           user: Principal = Depends(require_permission("settings.orchestration.configure"))):
    """Change any of the agent limits. Values are clamped to their allowed range and persisted to
    config/app.json (agent.*, context.compaction_threshold, memory.*)."""
    from core.agent_tools.limits import AGENT_LIMITS, CONTEXT_LIMITS, MEMORY_LIMITS
    sent = req.model_dump(exclude_none=True)
    if not sent:
        return JSONResponse({"error": "nothing to change"}, status_code=400)
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        return JSONResponse({"error": f"config/app.json unreadable: {e}"}, status_code=500)

    def clamp(v, spec):
        return max(spec[1], min(spec[2], type(spec[0])(v)))
    agent_cfg, ctx_cfg, mem_cfg = cfg.setdefault("agent", {}), cfg.setdefault("context", {}), cfg.setdefault("memory", {})
    changes = {}
    for k, v in sent.items():
        if k in AGENT_LIMITS:
            agent_cfg[k] = clamp(v, AGENT_LIMITS[k])
            changes[k] = agent_cfg[k]
        elif k == "executor_max_effort":
            if str(v).lower() not in ("none", "low", "medium", "high", "extra"):
                return JSONResponse({"error": "executor_max_effort must be none, low, medium, high or extra"}, status_code=400)
            agent_cfg[k] = str(v).lower()
            changes[k] = agent_cfg[k]
        elif k == "plan_required":
            if str(v).lower() not in ("auto", "always", "off"):
                return JSONResponse({"error": "plan_required must be auto, always or off"}, status_code=400)
            agent_cfg[k] = str(v).lower()
            changes[k] = agent_cfg[k]
        elif k == "mirror_to_workspace":
            agent_cfg[k] = bool(v)
            changes[k] = agent_cfg[k]
        elif k == "compaction_threshold":
            ctx_cfg[k] = round(clamp(v, CONTEXT_LIMITS[k]), 2)
            changes[k] = ctx_cfg[k]
        elif k == "memory_enabled":
            mem_cfg["enabled"] = bool(v)
            changes[k] = mem_cfg["enabled"]
        elif k == "memory_allow_cloud":
            mem_cfg["allow_cloud"] = bool(v)
            changes[k] = mem_cfg["allow_cloud"]
        elif k == "memory_file_max_bytes":
            mem_cfg["file_max_bytes"] = clamp(v, MEMORY_LIMITS["file_max_bytes"])
            changes[k] = mem_cfg["file_max_bytes"]
        elif k == "memory_max_files":
            mem_cfg["max_files"] = clamp(v, MEMORY_LIMITS["max_files"])
            changes[k] = mem_cfg["max_files"]
    try:
        write_app_config(cfg, CONFIG_FILE)
    except Exception as e:
        return JSONResponse({"error": f"config/app.json write failed: {e}"}, status_code=500)
    APP_CONFIG["agent"] = {**(APP_CONFIG.get("agent") or {}), **agent_cfg}
    APP_CONFIG["context"] = {**(APP_CONFIG.get("context") or {}), **ctx_cfg}
    APP_CONFIG["memory"] = {**(APP_CONFIG.get("memory") or {}), **mem_cfg}
    audit_log(user, action="agent.limits", resource="agent.limits",
              permission_key="settings.orchestration.configure", detail=changes)
    return {"ok": True, **_limits_view()}
