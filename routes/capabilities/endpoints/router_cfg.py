import json
from fastapi import Depends
from fastapi.responses import JSONResponse
from core import route_log
from core.audit import audit_log
from core.auth import Principal
from core.deps import require_permission
from core.router_tuner import run_tuner
from ..models import RouterSettingsReq
from ..router_helpers import _router_settings, _router_view, _validate_router_changes, _write_router

from .base import router


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
