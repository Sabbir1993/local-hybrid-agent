import json
from fastapi import Depends
from fastapi.responses import JSONResponse
from core.agent_library import library_status
from core.audit import audit_log
from core.auth import Principal
from core.config import CONFIG_FILE, write_app_config
from core.deps import get_current_user, require_permission
from core.small_model import APP_CONFIG
from ..models import AgentLibraryReq

from .base import router


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
