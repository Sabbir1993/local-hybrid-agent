"""
routes/capabilities.py - Tool capabilities, shell execution settings, and model routing status.
"""

import json
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.config import CONFIG_FILE
from core.small_model import (
    APP_CONFIG,
    small_models,
    needle_available,
    router_available,
    router_engine_name,
)
from core import cloud
from core.audit import audit_log
from core.auth import Principal
from core.deps import get_current_user, require_permission
from core.state import state
from core.agent_tools import get_active_project
from core.registry import registry
from core.web_tools import register_web_tools
from core.skills import register_skill_tools, load_skills
from core.agent_library import load_prompt_commands, load_agent_profiles, library_status, library_enabled
from core.plugins import plugins_status, load_plugins, unload_all
from core.mcp import mcp_status
from core.shell_tools import (
    register_shell_tools,
    shell_cfg,
)

router = APIRouter(tags=["capabilities"])

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


class CapToggleReq(BaseModel):
    section: str          # web | skills | mcp | plugins
    enabled: bool


class LibraryListReq(BaseModel):
    allow: Optional[list] = None
    deny: Optional[list] = None


class AgentLibraryReq(BaseModel):
    enabled: Optional[bool] = None
    default_policy: Optional[str] = None      # "deny" | "allow"
    agents: Optional[LibraryListReq] = None
    commands: Optional[LibraryListReq] = None


class ShellSettingsReq(BaseModel):
    ask_first: Optional[bool] = None
    allow_patterns: Optional[list] = None
    timeout_s: Optional[int] = None


@router.get("/control/capabilities")
async def capabilities_status():
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
            "servers": mcp_status(),
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
        },
        "total_tools": len(registry.schemas()),
    }


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
        cfg_path.write_text(_json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception as e:
        return JSONResponse({"error": f"config/app.json write failed: {e}"}, status_code=500)
    # live update
    APP_CONFIG["capabilities"]["shell"] = shell
    return {"ok": True, "shell": shell}


AGENT_STEPS_MIN, AGENT_STEPS_MAX = 5, 200


class AgentSettingsReq(BaseModel):
    max_steps: int


@router.post("/control/agent_settings")
async def agent_settings(req: AgentSettingsReq,
                         user: Principal = Depends(require_permission("settings.orchestration.configure"))):
    """Agent Task step cap (agent.max_steps); persists to config/app.json. Bounded on
    purpose - an uncapped run can hold the shared GPU indefinitely."""
    steps = max(AGENT_STEPS_MIN, min(AGENT_STEPS_MAX, int(req.max_steps)))
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        return JSONResponse({"error": f"config/app.json unreadable: {e}"}, status_code=500)
    old = cfg.setdefault("agent", {}).get("max_steps")
    cfg["agent"]["max_steps"] = steps
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception as e:
        return JSONResponse({"error": f"config/app.json write failed: {e}"}, status_code=500)
    APP_CONFIG.setdefault("agent", {})["max_steps"] = steps
    audit_log(user, action="agent.settings", resource="agent.max_steps",
              permission_key="settings.orchestration.configure", detail={"from": old, "to": steps})
    return {"ok": True, "max_steps": steps}


@router.get("/control/agent_library")
async def agent_library_get(user: Principal = Depends(get_current_user)):
    """Every Agent Library profile / prompt command with its allow/deny state and
    compatibility warnings (read-only for everyone; editing needs the permission)."""
    return library_status()


@router.post("/control/agent_library")
async def agent_library_update(req: AgentLibraryReq,
                               user: Principal = Depends(require_permission("settings.agents.configure"))):
    """Edit the org-wide Agent Library allow/deny policy; persists to config/app.json.
    Same pattern as /control/shell_settings. deny always beats allow."""
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        return JSONResponse({"error": f"config/app.json unreadable: {e}"}, status_code=500)
    lib = cfg.setdefault("agent_library", {})
    if req.enabled is not None:
        lib["enabled"] = bool(req.enabled)
    if req.default_policy is not None:
        if req.default_policy not in ("deny", "allow"):
            return JSONResponse({"error": "default_policy must be 'deny' or 'allow'"}, status_code=400)
        lib["default_policy"] = req.default_policy
    for kind in ("agents", "commands"):
        upd = getattr(req, kind)
        if upd is None:
            continue
        sect = lib.setdefault(kind, {"allow": [], "deny": []})
        for key in ("allow", "deny"):
            vals = getattr(upd, key)
            if vals is not None:
                sect[key] = sorted({str(v).strip() for v in vals if str(v).strip()})
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception as e:
        return JSONResponse({"error": f"config/app.json write failed: {e}"}, status_code=500)
    APP_CONFIG["agent_library"] = lib
    audit_log(user, action="agent_library.update", resource="agent_library",
              permission_key="settings.agents.configure",
              detail=req.model_dump(exclude_none=True))
    return {"ok": True, **library_status()}


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
    caps[req.section] = req.enabled
    try:
        cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
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



