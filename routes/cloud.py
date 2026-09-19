"""
routes/cloud.py - Cloud (OpenAI-compatible) provider + lane configuration API.

Everything here is UI-managed: providers (including API keys) and lane bindings
are persisted to config/providers.json (untracked by git), then applied live.
API keys are only ever returned masked, except on an explicit reveal request.
"""

from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core import cloud
from core.config import CLOUD_LANES, PROVIDERS_FILE
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


class ProbeReq(BaseModel):
    key: Optional[str] = None              # "<provider>/<model-id>"
    provider: Optional[str] = None
    model: Optional[str] = None
    prompt: Optional[str] = None


def _snapshot() -> dict:
    """Everything the settings card needs in one shot."""
    lanes = {}
    for lane in CLOUD_LANES:
        cm = cloud.cloud_lane(lane)
        lanes[lane] = (cm.info(lane) if cm else {"lane": lane, "kind": "local", "key": None})
    return {
        "providers": cloud.providers_public(),
        "models": [{
            "key": cm.key,
            "provider": cm.provider,
            "provider_name": cm.provider_name,
            "model": cm.model_id,
            "display": cm.display,
            "ctx": cm.ctx,
            "endpoint": cm.endpoint(),
        } for cm in cloud.cloud_models()],
        "bindings": cloud.cloud_bindings(),
        "lanes": lanes,
        "local": {
            "main": (state.profile.get("model_path") if state.profile else None),
            "main_loaded": bool(state.process is not None and state.process.poll() is None),
            "small": small_models.status(),
        },
        "providers_file": PROVIDERS_FILE.name,
    }


@router.get("/control/cloud")
async def cloud_status():
    """Providers (masked keys), cloud models, lane bindings, and local inventory."""
    try:
        return _snapshot()
    except Exception as e:
        return JSONResponse({"error": f"cloud config unreadable: {e}"}, status_code=500)


@router.get("/control/cloud/key")
async def cloud_key(provider: str):
    """Explicit reveal of one provider's API key (user pressed the eye button)."""
    entry = (cloud.providers().get(provider) or {})
    opts = entry.get("options") or {}
    return {"provider": provider, "api_key": str(opts.get("apiKey") or opts.get("api_key") or "")}


@router.post("/control/cloud/provider")
async def cloud_save_provider(req: ProviderReq):
    """Add or update a provider (base URL, API key, models). Persists + applies live."""
    try:
        cloud.save_provider(req.provider, {
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
    return {"ok": True, **_snapshot()}


@router.delete("/control/cloud/provider")
async def cloud_delete_provider(name: str):
    """Remove a provider and unbind any lane that pointed at it."""
    try:
        cloud.delete_provider(name)
    except Exception as e:
        return JSONResponse({"error": f"delete failed: {e}"}, status_code=500)
    return {"ok": True, **_snapshot()}


@router.delete("/control/cloud/model")
async def cloud_delete_model(provider: str, model: str):
    """Remove one model from a provider and unbind any lane pointing at it."""
    try:
        cloud.delete_model(provider, model)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": f"delete failed: {e}"}, status_code=500)
    return {"ok": True, **_snapshot()}


@router.post("/control/cloud/lanes")
async def cloud_set_lanes(req: LaneReq):
    """Bind lanes to a cloud model, or unbind them with {"clear": ["main"]}."""
    updates = {}
    for lane in CLOUD_LANES:
        v = getattr(req, lane)
        if v is not None:
            updates[lane] = v
    for lane in (req.clear or []):
        if lane in CLOUD_LANES:
            updates[lane] = None
    if req.fallback_local is not None:
        updates["fallback_local"] = req.fallback_local
    if req.routing_mode is not None:
        updates["routing_mode"] = req.routing_mode
    try:
        cloud.set_lanes(updates)
    except Exception as e:
        return JSONResponse({"error": f"save failed: {e}"}, status_code=500)
    return {"ok": True, **_snapshot()}


@router.post("/control/cloud/test")
async def cloud_test(req: ProbeReq):
    """One tiny completion against a configured provider/model (URL + key + model id)."""
    key = req.key or (f"{req.provider}/{req.model}" if req.provider and req.model else None)
    cm = cloud.get_cloud(key)
    if cm is None:
        return JSONResponse({"error": f"'{key}' is not a configured provider/model"}, status_code=404)
    result = await cloud.probe(cm, req.prompt or "ping")
    return {"key": cm.key, "label": cm.label(), **result}