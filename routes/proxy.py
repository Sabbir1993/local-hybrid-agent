"""
routes/proxy.py - OpenAI-compatible reverse proxy forwarding requests to the active llama-server.
"""

import asyncio
import sys
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse, JSONResponse

from core.state import state
from core.monitor import (
    _monitor_state,
    monitor_begin,
    monitor_end,
    extract_usage_from_stream,
    parse_cache_tokens,
)
from core.db import db_record_request
from core import cloud

router = APIRouter(tags=["proxy"])

_CHAT_PATHS = ("v1/chat/completions", "chat/completions")


async def _proxy_cloud(cm, path: str, request: Request, body: bytes):
    """Forward a chat-completions request straight to a cloud-bound main lane.

    Raises on failure so the caller can decide whether to fall back to the
    local lane (per config/providers.json -> cloud.fallback_local).
    """
    import json as _json
    client = cloud.CloudClient(cm)
    payload = _json.loads(body) if body else {}
    is_streaming = bool(payload.get("stream"))
    t0 = time.time()
    state.last_activity = time.time()
    rid = monitor_begin(path, is_streaming, body, model=cm.model_id, source="cloud", provider=cm.provider_name)

    try:
        return await _proxy_cloud_inner(client, cm, path, payload, is_streaming, rid, t0)
    except Exception:
        monitor_end(rid, 0, duration=time.time() - t0)
        raise


async def _proxy_cloud_inner(client, cm, path, payload, is_streaming, rid, t0):
    if is_streaming:
        # Probe the connection before committing to a StreamingResponse, so a
        # cloud auth/route failure can still fall back to local instead of
        # streaming a raw error body back to the client.
        probe = client.stream(json=payload)
        r0 = await probe.__aenter__()
        if r0.status_code >= 400:
            err_body = (await r0.aread())[:300]
            await probe.__aexit__(None, None, None)
            raise RuntimeError(f"HTTP {r0.status_code}: {err_body.decode('utf-8', 'replace')}")

        async def stream_gen():
            status = r0.status_code
            usage = None
            try:
                async for chunk in r0.aiter_bytes():
                    u = extract_usage_from_stream(chunk.decode("utf-8", "replace"), rid)
                    if u:
                        usage = u
                    yield chunk
            finally:
                await probe.__aexit__(None, None, None)
            dt = time.time() - t0
            ptoks = usage.get("prompt_tokens") if usage else None
            ctoks = usage.get("completion_tokens") if usage else None
            tps = (ctoks / dt) if usage and ctoks else None
            print(f"[server_manager] {path} (cloud:{cm.key}) streamed in {dt:.2f}s "
                  f"({ctoks or '?'} tok)")
            monitor_end(rid, status, ptoks, ctoks, tps, dt, source="cloud", provider=cm.provider_name)
            db_record_request(path, cm.model_id, ptoks, ctoks, tps, dt, None, True, status,
                              source="cloud", provider=cm.provider_name)
        return StreamingResponse(stream_gen(), media_type="text/event-stream")

    r = await client.post(json=payload)
    if r.status_code >= 400:
        monitor_end(rid, r.status_code, duration=time.time() - t0)
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
    dt = time.time() - t0
    ptoks = ctoks = tps = None
    try:
        data = r.json()
        usage = data.get("usage", {})
        ptoks = usage.get("prompt_tokens")
        ctoks = usage.get("completion_tokens")
        if ctoks and dt > 0:
            tps = ctoks / dt
    except Exception:
        pass
    monitor_end(rid, r.status_code, ptoks, ctoks, tps, dt, source="cloud", provider=cm.provider_name)
    db_record_request(path, cm.model_id, ptoks, ctoks, tps, dt, None, False, r.status_code,
                      source="cloud", provider=cm.provider_name)
    return JSONResponse(content=r.json() if r.headers.get("content-type", "").startswith("application/json") else {"raw": r.text},
                         status_code=r.status_code)


@router.api_route("/{path:path}", methods=["GET", "POST"])
async def proxy(path: str, request: Request):
    if path in ("favicon.ico", "index.html"):
        return JSONResponse({"error": "not found"}, status_code=404)

    # Main lane bound to the cloud: route chat-completions traffic there
    # instead of silently auto-starting a local llama-server. Other
    # llama.cpp-only endpoints (/slots, /metrics, ...) have no cloud
    # equivalent and still need the local process.
    cloud_main = cloud.cloud_lane("main") if path.lower() in _CHAT_PATHS else None
    if cloud_main is not None:
        body = await request.body()
        try:
            return await _proxy_cloud(cloud_main, path, request, body)
        except Exception as e:
            fb_enabled = bool(cloud.cloud_bindings().get("fallback_local", True))
            if not fb_enabled:
                return JSONResponse({
                    "error": {"message": f"Cloud request failed: {e}", "type": "cloud_request_failed"}
                }, status_code=502)
            print(f"[server_manager] cloud main lane failed ({e}) — falling back to local", file=sys.stderr)
            # fall through to the local path below

    if state.process is None or state.process.poll() is not None or state.client is None:
        target = state.profile_path or state.profile
        if target:
            try:
                print(f"[server_manager] proxy {path}: model not running — auto-starting...")
                await state.load_profile(target)
            except Exception as e:
                return JSONResponse({
                    "error": {
                        "message": f"Model failed to auto-start: {e}",
                        "type": "model_start_failed"
                    }
                }, status_code=503)
        else:
            return JSONResponse({
                "error": {
                    "message": "No model is loaded. Select a model from the dropdown and click ▶ to load it into GPU VRAM.",
                    "type": "model_not_loaded"
                }
            }, status_code=503)

    body = await request.body()
    t0 = time.time()
    state.last_activity = time.time()
    is_streaming = b'"stream":true' in body or b'"stream": true' in body
    model_name = state.profile.get("model_path") if state.profile else None
    clean_model_name = Path(model_name).name.replace(".gguf", "") if model_name else None
    rid = monitor_begin(path, is_streaming, body, model=clean_model_name, source="local")

    async def do_request():
        return await state.client.request(
            request.method, f"/{path}",
            content=body,
            headers={k: v for k, v in request.headers.items() if k.lower() != "host"},
            params=request.query_params,
        )

    if is_streaming:
        async def stream_gen():
            status = 500
            usage = None
            try:
                async with state.client.stream(
                    request.method, f"/{path}", content=body,
                    headers={k: v for k, v in request.headers.items() if k.lower() != "host"},
                    params=request.query_params,
                ) as r:
                    status = r.status_code
                    async for chunk in r.aiter_bytes():
                        u = extract_usage_from_stream(chunk.decode("utf-8", "replace"), rid)
                        if u:
                            usage = u
                        yield chunk
                dt = time.time() - t0
            except (asyncio.CancelledError, GeneratorExit):
                dt = time.time() - t0
                req = _monitor_state["active"].get(rid)
                ptoks = usage.get("prompt_tokens") if usage else None
                ctoks = usage.get("completion_tokens") if usage else (req["gen_tokens"] if req else None)
                tps = (ctoks / dt) if ctoks else None
                monitor_end(rid, 499, ptoks, ctoks, tps, dt, source="local")
                db_record_request(path, clean_model_name, ptoks, ctoks, tps, dt, None, True, 499, source="local")
                return
            dt = time.time() - t0
            ptoks = usage.get("prompt_tokens") if usage else None
            ctoks = usage.get("completion_tokens") if usage else None
            tps = (ctoks / dt) if usage and ctoks else None
            u = usage or {}
            pcached, ccached = parse_cache_tokens(u)
            is_orch = bool("orchestrator" in (clean_model_name or "").lower() or "orchestrator" in path.lower())
            print(f"[server_manager] {path} streamed in {dt:.2f}s "
                  f"({ctoks or '?'} tok{'' if ctoks is None else ''})")
            monitor_end(rid, status, ptoks, ctoks, tps, dt, prompt_cached=pcached, completion_cached=ccached, source="local")
            db_record_request(path, clean_model_name, ptoks, ctoks, tps, dt, None, True, status,
                              prompt_cached_tokens=pcached, completion_cached_tokens=ccached, is_orchestrator=is_orch,
                              source="local")
        return StreamingResponse(stream_gen(), media_type="text/event-stream")

    r = await do_request()
    dt = time.time() - t0
    ptoks = ctoks = tps = prompt_tps = None
    pcached = ccached = 0
    is_orch = bool("orchestrator" in (clean_model_name or "").lower() or "orchestrator" in path.lower())
    try:
        data = r.json()
        usage = data.get("usage", {})
        ptoks = usage.get("prompt_tokens")
        ctoks = usage.get("completion_tokens")
        if ctoks and dt > 0:
            tps = ctoks / dt
            print(f"[server_manager] {path}: {ctoks} tokens in {dt:.2f}s "
                  f"= {tps:.1f} t/s")
        tim = data.get("timings", {})
        if isinstance(tim, dict) and tim.get("prompt_per_second"):
            prompt_tps = tim.get("prompt_per_second")
        pcached, ccached = parse_cache_tokens(usage, tim)
    except Exception:
        pass
    monitor_end(rid, r.status_code, ptoks, ctoks, tps, dt, prompt_tps, prompt_cached=pcached, completion_cached=ccached, source="local")
    db_record_request(path, clean_model_name, ptoks, ctoks, tps, dt, prompt_tps, False, r.status_code,
                      prompt_cached_tokens=pcached, completion_cached_tokens=ccached, is_orchestrator=is_orch,
                      source="local")
    return JSONResponse(content=r.json() if r.headers.get("content-type", "").startswith("application/json") else {"raw": r.text},
                         status_code=r.status_code)


