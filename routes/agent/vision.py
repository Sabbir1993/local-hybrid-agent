import json
import sys
import time
from pathlib import Path
from fastapi import Depends
from fastapi.responses import JSONResponse
from core.agent_tools.limits import lane_output_cap
from core.auth import Principal
from core.deps import get_current_user, require_permission
from core.db import db_record_request
from core.small_model import small_models
from core.state import state
from core import cloud
from core.monitor import monitor_begin, monitor_end

from .base import router
from .models import VisionReq


@router.post("/agent/vision")
async def agent_vision(req: VisionReq, user: Principal = Depends(get_current_user)):
    payload = {
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": req.question},
                {"type": "image_url", "image_url": {"url": f"data:{req.mime};base64,{req.image_b64}"}},
            ],
        }],
        "max_tokens": lane_output_cap("vision"),   # Settings -> vision_max_tokens (was a fixed 400)
        "temperature": 0.1,
    }
    body_bytes = json.dumps({"messages": [req.question]}).encode()

    # ── Priority 1: Main Model if capable of vision ───────────────────────
    # If the main model (local with --mmproj) is running and vision-capable,
    # use it directly for vision tasks without spawning a secondary model.
    # "Reading images" job (core/lanes.py): an explicit mapping in Settings ->
    # Models skips the main model and goes straight to that job's route
    from core import lanes
    vision_route = lanes.targets("vision", user.id)
    vision_explicit = "vision" in cloud.role_map(user.id)
    main_ready = (state.process is not None and state.process.poll() is None and state.client is not None)
    first = vision_route[0] if vision_route else None
    picked_main = bool(first and first.lane == "main" and not first.is_cloud)   # explicit pick of main
    main_vision = (main_ready and bool((state.profile or {}).get("vision_capable"))
                   and (not vision_explicit or picked_main))
    if main_vision:
        model_name = Path((state.profile or {}).get("model_path", "")).name or "Main LLM (vision)"
        model_name = model_name.replace(".gguf", "")
        rid = monitor_begin("agent/vision", False, body_bytes, model=model_name, source="local")
        t0 = time.time()
        try:
            r = await state.client.post("/v1/chat/completions", json=payload, timeout=None)
            state.last_activity = time.time()
            data = r.json()
            usage = data.get("usage") or {}
            ptoks, ctoks = usage.get("prompt_tokens"), usage.get("completion_tokens")
            dt = time.time() - t0
            monitor_end(rid, 200, prompt_tokens=ptoks, completion_tokens=ctoks,
                        tps=(ctoks / dt if ctoks and dt > 0 else None), duration=dt,
                        model=model_name, source="local")
            db_record_request("agent/vision", model_name, ptoks, ctoks,
                               (ctoks / dt if ctoks and dt > 0 else None), dt, None, False, 200,
                               is_orchestrator=True, source="local")
            return {"description": (data.get("choices") or [{}])[0].get("message", {}).get("content") or "",
                    "lane": "main", "model": model_name}
        except Exception as e:
            monitor_end(rid, 500, duration=time.time() - t0, source="local")
            print(f"[agent/vision] main model vision failed ({model_name}): {e} - falling back to vision model", file=sys.stderr)

    # ── Priority 2: Cloud Vision Lane (or override) ───────────────────────
    cm = None
    if req.cloud_model_override:
        cm = cloud.get_cloud(req.cloud_model_override, user.id)
    if not cm:
        cm = next((t.cm for t in vision_route if t.is_cloud), None)
    if cm:
        rid = monitor_begin("agent/vision", False, body_bytes, model=cm.model_id, source="cloud", provider=cm.provider_name)
        t0 = time.time()
        try:
            r = await cloud.CloudClient(cm).post("/v1/chat/completions", json=payload, timeout=None)
            data = r.json()
            usage = data.get("usage") or {}
            ptoks, ctoks = usage.get("prompt_tokens"), usage.get("completion_tokens")
            dt = time.time() - t0
            monitor_end(rid, 200, prompt_tokens=ptoks, completion_tokens=ctoks,
                        tps=(ctoks / dt if ctoks and dt > 0 else None), duration=dt,
                        model=cm.model_id, source="cloud", provider=cm.provider_name)
            db_record_request("agent/vision", cm.model_id, ptoks, ctoks,
                               (ctoks / dt if ctoks and dt > 0 else None), dt, None, False, 200,
                               is_orchestrator=True, source="cloud", provider=cm.provider_name)
            return {"description": (data.get("choices") or [{}])[0].get("message", {}).get("content") or "",
                    "lane": "cloud", "model": cm.display, "provider": cm.provider_name}
        except Exception as e:
            monitor_end(rid, 502, duration=time.time() - t0, source="cloud", provider=cm.provider_name)
            print(f"[agent/vision] cloud vision lane failed ({cm.key}): {e} - falling back to small vision model", file=sys.stderr)

    # ── Priority 3: Dedicated small vision model ───────────────────────────
    local_t = next((t for t in vision_route if not t.is_cloud and t.available()), None)
    inst = small_models.instances.get(local_t.lane if local_t else "vision")
    if inst is None or not inst.available:
        return JSONResponse({"error": "vision model not configured (config/app.json small_models.vision.model/mmproj)"}, status_code=400)
    model_name = inst.model_path.name if inst.model_path else "vision"
    rid = monitor_begin("agent/vision", False, body_bytes, model=model_name, source="local")
    t0 = time.time()
    try:
        await inst.ensure_loaded()
        r = await inst.client.post("/v1/chat/completions", json=payload, timeout=None)
        inst.last_used = time.time()
        data = r.json()
        usage = data.get("usage") or {}
        ptoks, ctoks = usage.get("prompt_tokens"), usage.get("completion_tokens")
        dt = time.time() - t0
        monitor_end(rid, 200, prompt_tokens=ptoks, completion_tokens=ctoks,
                    tps=(ctoks / dt if ctoks and dt > 0 else None), duration=dt,
                    model=model_name, source="local")
        db_record_request("agent/vision", model_name, ptoks, ctoks,
                           (ctoks / dt if ctoks and dt > 0 else None), dt, None, False, 200,
                           is_orchestrator=True, source="local")
        return {"description": (data.get("choices") or [{}])[0].get("message", {}).get("content") or "",
                "lane": "local", "model": model_name}
    except Exception as e:
        monitor_end(rid, 500, duration=time.time() - t0, source="local")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/agent/unload_small_models")
async def unload_small_models_endpoint(user: Principal = Depends(require_permission("model.local.load"))):
    small_models.unload_all()
    return {"ok": True, "unloaded": True}
