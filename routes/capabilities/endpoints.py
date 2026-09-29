"""All capability and router endpoints."""

import json
import re

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from core import cloud, route_log
from core.agent_library import library_enabled, library_status, load_agent_profiles, load_prompt_commands
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
from core.small_model import APP_CONFIG, needle_available, router_available, router_engine_name, small_models
from core.state import state
from core.web_tools import register_web_tools
from core.router_tuner import run_tuner
from .models import (
    AGENT_STEPS_MAX,
    AGENT_STEPS_MIN,
    AgentLibraryReq,
    AgentSettingsReq,
    CapToggleReq,
    RouterSettingsReq,
    ShellSettingsReq,
)
from .router_helpers import _router_view, _validate_router_changes, _write_router

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
    if req.multi_lanes is not None:
        from core.lanes import registry, kind_ok
        reg = registry()
        ml = {k: v for k, v in req.multi_lanes.items() if k in ("backend", "frontend")}
        if any(v not in reg or not kind_ok("chat", reg[v]["kind"]) for v in ml.values()):
            return JSONResponse({"error": "multi_lanes values must name a text model, e.g. 'main' or 'executor'"},
                                status_code=400)
        lib["multi_lanes"] = {**(lib.get("multi_lanes") or {}), **ml}
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
        write_app_config(cfg, CONFIG_FILE)
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


@router.get("/control/router")
async def router_get(user: Principal = Depends(require_permission("usage.report.view"))):
    """Routing rules, per-category/lane usage stats and pending tuner suggestions."""
    return _router_view()


@router.post("/control/router")
async def router_update(req: RouterSettingsReq,
                        user: Principal = Depends(require_permission("settings.router.configure"))):
    changes, err = _validate_router_changes(req.model_dump(exclude_none=True))
    if err:
        return JSONResponse({"error": err}, status_code=400)
    if not changes:
        return JSONResponse({"error": "nothing to change"}, status_code=400)
    old, resp = _write_router(changes)
    if resp:
        return resp
    audit_log(user, action="router.update", resource="router", permission_key="settings.router.configure",
              detail={"from": old, "to": changes})
    return {"ok": True, **_router_view()}


@router.post("/control/router/tune")
async def router_tune(user: Principal = Depends(require_permission("settings.router.configure"))):
    """Run the usage analysis now (it only creates pending suggestions)."""
    ids = run_tuner()
    audit_log(user, action="router.tune.run", resource="router", permission_key="settings.router.configure",
              detail={"suggestions": ids})
    return {"ok": True, "new_or_updated": ids, **_router_view()}


@router.post("/control/router/suggestions/{sid}/{decision}")
async def router_suggestion_decide(sid: int, decision: str,
                                   user: Principal = Depends(require_permission("settings.router.configure"))):
    if decision not in ("apply", "dismiss"):
        return JSONResponse({"error": "decision must be apply or dismiss"}, status_code=400)
    sug = route_log.get_suggestion(sid)
    if not sug or sug["status"] != "pending":
        return JSONResponse({"error": "suggestion not found or already decided"}, status_code=404)
    who = getattr(user, "username", None) or str(user.id)
    if decision == "dismiss":
        route_log.decide_suggestion(sid, "dismissed", who)
        audit_log(user, action="router.tune.dismiss", resource=sug["key"], permission_key="settings.router.configure",
                  detail={"suggestion": sid, "proposed": sug["proposed"]})
        return {"ok": True, **_router_view()}
    changes, err = _validate_router_changes({sug["key"]: sug["proposed"]})
    if err:
        return JSONResponse({"error": err}, status_code=400)
    # stale guard: the setting changed since the tuner looked at it
    live = _router_settings().get(sug["key"])
    if json.dumps(live, sort_keys=True) != json.dumps(sug["current"], sort_keys=True):
        route_log.decide_suggestion(sid, "stale", who)
        return JSONResponse({"error": "this setting changed since the suggestion was made; run the tuner again"},
                            status_code=409)
    old, resp = _write_router(changes)
    if resp:
        return resp
    route_log.decide_suggestion(sid, "applied", who)
    audit_log(user, action="router.tune.apply", resource=sug["key"], permission_key="settings.router.configure",
              detail={"suggestion": sid, "from": old, "to": changes, "evidence": sug["evidence"]})
    return {"ok": True, **_router_view()}
