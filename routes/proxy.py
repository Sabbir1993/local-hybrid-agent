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

router = APIRouter(tags=["proxy"])

@router.api_route("/{path:path}", methods=["GET", "POST"])
async def proxy(path: str, request: Request):
    if path in ("favicon.ico", "index.html"):
        return JSONResponse({"error": "not found"}, status_code=404)

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
    rid = monitor_begin(path, is_streaming, body, model=clean_model_name)

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
                monitor_end(rid, 499, ptoks, ctoks, tps, dt)
                db_record_request(path, model_name, ptoks, ctoks, tps, dt, None, True, 499)
                return
            dt = time.time() - t0
            ptoks = usage.get("prompt_tokens") if usage else None
            ctoks = usage.get("completion_tokens") if usage else None
            tps = (ctoks / dt) if usage and ctoks else None
            u = usage or {}
            pcached, ccached = parse_cache_tokens(u)
            is_orch = bool("orchestrator" in (model_name or "").lower() or "orchestrator" in path.lower())
            print(f"[server_manager] {path} streamed in {dt:.2f}s "
                  f"({ctoks or '?'} tok{'' if ctoks is None else ''})")
            monitor_end(rid, status, ptoks, ctoks, tps, dt, prompt_cached=pcached, completion_cached=ccached)
            db_record_request(path, model_name, ptoks, ctoks, tps, dt, None, True, status,
                              prompt_cached_tokens=pcached, completion_cached_tokens=ccached, is_orchestrator=is_orch)
        return StreamingResponse(stream_gen(), media_type="text/event-stream")

    r = await do_request()
    dt = time.time() - t0
    ptoks = ctoks = tps = prompt_tps = None
    pcached = ccached = 0
    is_orch = bool("orchestrator" in (model_name or "").lower() or "orchestrator" in path.lower())
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
    monitor_end(rid, r.status_code, ptoks, ctoks, tps, dt, prompt_tps, prompt_cached=pcached, completion_cached=ccached)
    db_record_request(path, model_name, ptoks, ctoks, tps, dt, prompt_tps, False, r.status_code,
                      prompt_cached_tokens=pcached, completion_cached_tokens=ccached, is_orchestrator=is_orch)
    return JSONResponse(content=r.json() if r.headers.get("content-type", "").startswith("application/json") else {"raw": r.text},
                         status_code=r.status_code)


