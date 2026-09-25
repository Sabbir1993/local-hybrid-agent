"""
routes/cloud.py - Cloud (OpenAI-compatible) provider + lane configuration API.

Everything here is per-user: providers (including API keys) and lane bindings
are persisted to config/providers/user_<id>.json (untracked by git, never
readable by any other user), then applied live for that user's own chat/agent
requests. API keys are only ever returned masked, except on an explicit
reveal request.
"""

from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core import cloud
from core.audit import audit_log
from core.auth import Principal
from core.config import CLOUD_LANES
from core.deps import get_current_user
from core.small_model import small_models
from core.state import state

router = APIRouter(tags=["cloud"])


class ProviderModel(BaseModel):
    id: str
    name: Optional[str] = None
    ctx: Optional[int] = None


class ProviderReq(BaseModel):
    provider: str
    name: Optional[str] = None
    npm: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    chat_path: Optional[str] = None
    extra_headers: Optional[dict] = None
    extra_body: Optional[dict] = None
    stream_options: Optional[bool] = None
    models: Optional[list[ProviderModel]] = None


class LaneReq(BaseModel):
    main: Optional[str] = None
    executor: Optional[str] = None
    vision: Optional[str] = None
    fallback_local: Optional[bool] = None
    routing_mode: Optional[str] = None     # "auto" | "custom"
    clear: Optional[list[str]] = None      # lanes to unbind explicitly
    set: Optional[dict[str, Optional[str]]] = None   # {lane: "<provider>/<model>"} for any lane


class ProbeReq(BaseModel):
    key: Optional[str] = None              # "<provider>/<model-id>"
    provider: Optional[str] = None
    model: Optional[str] = None
    prompt: Optional[str] = None


def _snapshot(user_id: int) -> dict:
    """Everything the settings card needs in one shot, scoped to this user's own providers."""
    lanes = {}
    for lane in CLOUD_LANES:
        cm = cloud.cloud_lane(lane, user_id)
        lanes[lane] = (cm.info(lane) if cm else {"lane": lane, "kind": "local", "key": None})
    return {
        "providers": cloud.providers_public(user_id),
        "models": [{
            "key": cm.key,
            "provider": cm.provider,
            "provider_name": cm.provider_name,
            "model": cm.model_id,
            "display": cm.display,
            "ctx": cm.ctx,
            "endpoint": cm.endpoint(),
        } for cm in cloud.cloud_models(user_id)],
        "bindings": cloud.cloud_bindings(user_id),
        "lanes": lanes,
        "local": {
            "main": (state.profile.get("model_path") if state.profile else None),
            "main_loaded": bool(state.process is not None and state.process.poll() is None),
            "small": small_models.status(),
        },
        "providers_file": f"config/providers/user_{user_id}.json",
    }


@router.get("/control/cloud")
async def cloud_status(user: Principal = Depends(get_current_user)):
    """This user's own providers (masked keys), cloud models, lane bindings, and local inventory."""
    try:
        return _snapshot(user.id)
    except Exception as e:
        return JSONResponse({"error": f"cloud config unreadable: {e}"}, status_code=500)


@router.get("/control/cloud/key")
async def cloud_key(provider: str, user: Principal = Depends(get_current_user)):
    """Explicit reveal of one of THIS user's own provider API keys (eye button)."""
    entry = (cloud.providers(user.id).get(provider) or {})
    opts = entry.get("options") or {}
    audit_log(user, action="cloud.key.reveal", resource=provider, result="allow")
    return {"provider": provider, "api_key": str(opts.get("apiKey") or opts.get("api_key") or "")}


@router.post("/control/cloud/provider")
async def cloud_save_provider(req: ProviderReq, user: Principal = Depends(get_current_user)):
    """Add or update a provider in the caller's own config. Persists + applies live."""
    try:
        cloud.save_provider(user.id, req.provider, {
            "name": req.name, "npm": req.npm, "base_url": req.base_url,
            "api_key": req.api_key, "chat_path": req.chat_path,
            "extra_headers": req.extra_headers,
            "extra_body": req.extra_body, "stream_options": req.stream_options,
            "models": [m.model_dump() for m in req.models] if req.models else None,
        })
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"save failed: {e}"}, status_code=500)
    audit_log(user, action="cloud.provider.save", resource=req.provider, result="allow")
    return {"ok": True, **_snapshot(user.id)}


@router.delete("/control/cloud/provider")
async def cloud_delete_provider(name: str, user: Principal = Depends(get_current_user)):
    """Remove one of the caller's own providers and unbind any lane that pointed at it."""
    try:
        cloud.delete_provider(user.id, name)
    except Exception as e:
        return JSONResponse({"error": f"delete failed: {e}"}, status_code=500)
    audit_log(user, action="cloud.provider.delete", resource=name, result="allow")
    return {"ok": True, **_snapshot(user.id)}


@router.delete("/control/cloud/model")
async def cloud_delete_model(provider: str, model: str, user: Principal = Depends(get_current_user)):
    """Remove one model from one of the caller's own providers and unbind any lane pointing at it."""
    try:
        cloud.delete_model(user.id, provider, model)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": f"delete failed: {e}"}, status_code=500)
    audit_log(user, action="cloud.model.delete", resource=f"{provider}/{model}", result="allow")
    return {"ok": True, **_snapshot(user.id)}


@router.post("/control/cloud/lanes")
async def cloud_set_lanes(req: LaneReq, user: Principal = Depends(get_current_user)):
    """Bind the caller's own lanes to a cloud model, or unbind them with {"clear": ["main"]}."""
    updates = {}
    for lane in CLOUD_LANES:
        v = getattr(req, lane)
        if v is not None:
            updates[lane] = v
    for lane, v in (req.set or {}).items():
        if lane in CLOUD_LANES or lane in cloud.user_lanes(user.id):
            updates[lane] = v
    for lane in (req.clear or []):
        if lane in CLOUD_LANES:
            updates[lane] = None
    if req.fallback_local is not None:
        updates["fallback_local"] = req.fallback_local
    if req.routing_mode is not None:
        updates["routing_mode"] = req.routing_mode
    try:
        cloud.set_lanes(user.id, updates)
    except Exception as e:
        return JSONResponse({"error": f"save failed: {e}"}, status_code=500)
    audit_log(user, action="cloud.lanes.set", detail=updates, result="allow")
    return {"ok": True, **_snapshot(user.id)}


class SelectMainReq(BaseModel):
    key: str


@router.post("/control/cloud/select_main")
async def cloud_select_main(req: SelectMainReq, user: Principal = Depends(get_current_user)):
    """Any authenticated user may pick their own main/orchestrator model --
    cloud provider/model choice is fully user-managed, never exposes
    base_url/api_key in the response."""
    cm = cloud.get_cloud(req.key, user.id)
    if cm is None:
        return JSONResponse({"error": f"'{req.key}' is not available"}, status_code=404)
    cloud.set_lanes(user.id, {"main": cm.key})
    audit_log(user, action="model.load", resource=cm.key, detail={"cloud": True, "select_main": True}, result="allow")
    return {"ok": True, "key": cm.key, "display": cm.display}


@router.post("/control/cloud/test")
async def cloud_test(req: ProbeReq, user: Principal = Depends(get_current_user)):
    """One tiny completion against one of the caller's own configured provider/models."""
    key = req.key or (f"{req.provider}/{req.model}" if req.provider and req.model else None)
    cm = cloud.get_cloud(key, user.id)
    if cm is None:
        return JSONResponse({"error": f"'{key}' is not a configured provider/model"}, status_code=404)
    result = await cloud.probe(cm, req.prompt or "ping")
    return {"key": cm.key, "label": cm.label(), **result}
