import json
import re
from fastapi import Depends
from fastapi.responses import JSONResponse
from core import cloud
from core.agent_library import library_enabled, load_agent_profiles, load_prompt_commands
from core.agent_tools import get_active_project
from core.audit import audit_log
from core.auth import Principal
from core.config import CONFIG_FILE, write_app_config
from core.deps import get_current_user, require_permission
from core.mcp import mcp_status
from core.plugins import load_plugins, plugins_status, unload_all
from core.registry import registry
from core.shell_tools import register_shell_tools, shell_cfg
from core.skills import load_skills, register_skill_tools
from core.small_model import APP_CONFIG, router_available, router_engine_name, small_models
from core.state import state
from core.web_tools import register_web_tools
from ..models import AGENT_STEPS_MAX, AGENT_STEPS_MIN, CapToggleReq

from .base import router


@router.get("/control/models")
async def models_status(user: Principal = Depends(get_current_user)):
    cm_main = cloud.cloud_lane("main", user.id)
    cm_exec = cloud.cloud_lane("executor", user.id)
    cm_vision = cloud.cloud_lane("vision", user.id)
    local_exec = small_models.status()
    s = {
        "main": {
            "loaded": state.process is not None and state.process.poll() is None,
            "model": state.profile.get("model_path") if state.profile else None,
            "pid": state.process.pid if state.process and state.process.poll() is None else None,
            "source": "cloud" if cm_main else "local",
            "cloud": cm_main.key if cm_main else None,
        },
        "small": local_exec,
        "lanes": {
            "main": cm_main.info("main") if cm_main else {"lane": "main", "source": "local",
                                                          "model": (state.profile or {}).get("model_path")},
            "executor": cm_exec.info("executor") if cm_exec else {
                "lane": "executor", "source": "local",
                "model": (local_exec.get("executor") or {}).get("model")},
            "vision": cm_vision.info("vision") if cm_vision else {
                "lane": "vision", "source": "local",
                "model": (local_exec.get("vision") or {}).get("model")},
        },
        "cloud": {
            "bindings": cloud.cloud_bindings(user.id),
            "providers": len(cloud.providers(user.id)),
            "models": len(cloud.cloud_models(user.id)),
        },
        "router": {
            "enabled": bool(APP_CONFIG["router"].get("enabled", True)),
            "engine": router_engine_name(),
            "available": router_available(),
            "confidence_threshold": APP_CONFIG["router"].get("confidence_threshold", 0.7),
        },
        "project": get_active_project(),
    }
    return s


@router.get("/control/capabilities")
async def capabilities_status(user: Principal = Depends(get_current_user)):
    caps = APP_CONFIG.get("capabilities", {})
    skills = load_skills() if caps.get("skills") else {}
    sh = shell_cfg()
    return {
        "web": {
            "enabled": bool(caps.get("web")),
            "tools": [{"name": t.name, "description": t.schema["function"]["description"][:120]}
                      for t in registry.list(source="web")],
        },
        "skills": {
            "enabled": bool(caps.get("skills")),
            "items": [{"name": s["name"], "description": s["description"]} for s in skills.values()],
        },
        "mcp": {
            "enabled": bool(caps.get("mcp")),
            "servers": mcp_status(user.id),   # global + this user's personal servers
        },
        "agent_library": {
            "enabled": library_enabled(),
            "agents": [{"name": p["name"], "description": p["description"]} for p in load_agent_profiles().values()],
            "commands": [{"name": c["name"], "description": c["description"],
                          "argument_hint": c["argument_hint"]} for c in load_prompt_commands().values()],
        },
        "plugins": {
            "enabled": bool(caps.get("plugins")),
            "items": plugins_status(),
        },
        "shell": {
            "enabled": bool(sh.get("enabled")),
            "ask_first": bool(sh.get("ask_first", True)),
            "timeout_s": sh.get("timeout_s", 60),
            "allow_patterns": sh.get("allow_patterns", []),
        },
        "agent": {
            "max_steps": APP_CONFIG.get("agent", {}).get("max_steps", 60),
            "min": AGENT_STEPS_MIN, "max": AGENT_STEPS_MAX,
            "run_timeout_s": APP_CONFIG.get("agent", {}).get("run_timeout_s", 1800),
            "timeout_min": 0, "timeout_max": 1440,
        },
        "total_tools": len(registry.schemas()),
    }


@router.post("/control/capabilities")
async def capabilities_toggle(req: CapToggleReq,
                              user: Principal = Depends(require_permission("settings.orchestration.configure"))):
    """Toggle a capability section on/off; persists to config/app.json.
    Global for every user (e.g. enables the shell tool), hence the permission."""
    cfg_path = CONFIG_FILE
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception as e:
        return JSONResponse({"error": f"config/app.json unreadable: {e}"}, status_code=500)
    caps = cfg.setdefault("capabilities", {})
    # on/off switches only: a free-form key could replace a structured section
    # (shell, mcp_servers, mcp_allowed_commands...) with a bare boolean
    if not re.fullmatch(r"[a-z_]{1,32}", req.section or "") or not isinstance(caps.get(req.section, False), bool):
        return JSONResponse({"error": f"'{req.section}' is not an on/off capability"}, status_code=400)
    caps[req.section] = req.enabled
    try:
        write_app_config(cfg, CONFIG_FILE)
    except Exception as e:
        return JSONResponse({"error": f"config/app.json write failed: {e}"}, status_code=500)
    # apply live
    audit_log(user, action="capabilities.toggle", resource=req.section,
              permission_key="settings.orchestration.configure", detail={"enabled": req.enabled})
    caps_live = APP_CONFIG.setdefault("capabilities", {})
    if req.section == "shell":
        caps_live.setdefault("shell", {})["enabled"] = req.enabled
        if req.enabled:
            register_shell_tools()
        else:
            registry.set_source_enabled("shell", False)
        return {"ok": True, "section": req.section, "enabled": req.enabled}
    caps_live[req.section] = req.enabled
    if req.section == "plugins":
        # plugin tools register as plugin:<name>, so load/unload rather than toggle a source
        if req.enabled:
            load_plugins()
        else:
            unload_all()
        return {"ok": True, "section": req.section, "enabled": req.enabled}
    registry.set_source_enabled(req.section, req.enabled)
    if req.section == "web" and req.enabled:
        register_web_tools()
    if req.section == "skills" and req.enabled:
        register_skill_tools()
    return {"ok": True, "section": req.section, "enabled": req.enabled}
