"""Main proxy endpoint — routes requests to cloud or local llama-server."""

import asyncio
import json
import sys
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from core import cloud, input_guard
from core.audit import audit_log
from core.auth import Principal, user_has_permission
from core.db import db_record_request
from core.deps import require_permission
from core.limits import check_cloud_request_quota
from core.monitor import (
    _monitor_state,
    extract_usage_from_stream,
    monitor_begin,
    monitor_end,
    parse_cache_tokens,
)
from routes.common.llm_stream import admission
from core.state import state
from .cloud_proxy import _proxy_cloud
from .constants import _ALLOWED_PATHS, _CHAT_PATHS, _upstream_headers
from .guard import _SSERedactor, _audit_redaction, _guarded_json_response, _prompt_texts

router = APIRouter(tags=["proxy"])


@router.api_route("/{path:path}", methods=["GET", "POST"])
async def proxy(path: str, request: Request, user: Principal = Depends(require_permission("chat.use"))):
    if path.lower().strip("/") not in _ALLOWED_PATHS:
        return JSONResponse({"error": "not found"}, status_code=404)

    # Main lane bound to the cloud: route chat-completions traffic there
    # instead of silently auto-starting a local llama-server. Other
    # llama.cpp-only endpoints (/slots, /metrics, ...) have no cloud
    # equivalent and still need the local process.
    cloud_main = cloud.cloud_lane("main", user.id) if path.lower() in _CHAT_PATHS else None

    # Input sanitizer: same rules as /chat and /agent -- the raw API must not be
    # a way around them (cloud_only rules only fire when the lane is cloud).
    body = await request.body()
    if request.method == "POST" and body:
        try:
            payload = json.loads(body)
        except Exception:
            payload = None
        if isinstance(payload, dict):
            _hit = await input_guard.check_async(_prompt_texts(payload), user,
                                                 any_cloud_lane=cloud_main is not None)
            if _hit:
                audit_log(user, action="input_guard.block", resource=_hit.get("name"),
                          detail={"scope": _hit.get("scope"), "endpoint": f"proxy/{path}",
                                  "pattern": _hit.get("_matched_pattern")}, result="deny")
                return JSONResponse({"error": {"message": _hit.get("message"),
                                               "type": "input_guard_block"}}, status_code=403)

    if cloud_main is not None:
        # A cloud call costs money and the audit log is the only per-user record we have:
        # core/db/usage.py's requests table carries no user_id, so a token/spend cap cannot be
        # computed yet (see core/limits.py::daily_cloud_requests).
        if (why := check_cloud_request_quota(user.id)) is not None:
            audit_log(user, action="cloud.quota", resource=path, result="deny",
                      detail={"reason": why})
            return JSONResponse({"error": {"message": why, "type": "cloud_quota_exceeded"}},
                                status_code=429)
        try:
            return await _proxy_cloud(cloud_main, path, request, body, user)
        except Exception as e:
            fb_enabled = bool(cloud.cloud_bindings(user.id).get("fallback_local", True))
            if not fb_enabled:
                return JSONResponse({
                    "error": {"message": f"Cloud request failed: {e}", "type": "cloud_request_failed"}
                }, status_code=502)
            print(f"[server_manager] cloud main lane failed ({e}) — falling back to local", file=sys.stderr)
            # fall through to the local path below

    if state.process is None or state.process.poll() is not None or state.client is None:
        target = state.profile_path or state.profile
        if target and not user_has_permission(user, "model.local.load"):
            return JSONResponse({"error": {"message": "No model is running and you lack permission to start one.",
                                           "type": "model_not_loaded"}}, status_code=503)
        if target:
            try:
                print(f"[server_manager] proxy {path}: model not running — auto-starting...")
                await state.ensure_running(target)
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
            headers=_upstream_headers(request),
            params=request.query_params,
        )

    if is_streaming:
        async def stream_gen():
            status = 500
            usage = None
            sse = _SSERedactor(user, False)
            # The gate is held for the life of the stream. llama-server queues by slot, but the
            # number of *requests* waiting on it was previously unbounded through /v1/*, so one
            # user could occupy every slot through the raw API. Released in `finally`, so a
            # client that disconnects mid-stream hands the slot back.
            sem = admission.hold()
            await sem.__aenter__()
            try:
                try:
                    async with state.client.stream(
                        request.method, f"/{path}", content=body,
                        headers=_upstream_headers(request),
                        params=request.query_params,
                    ) as r:
                        status = r.status_code
                        async for chunk in r.aiter_bytes():
                            u = extract_usage_from_stream(chunk.decode("utf-8", "replace"), rid)
                            if u:
                                usage = u
                            yield sse.feed(chunk) if sse.active else chunk
                        if sse.active:
                            yield sse.close()
                    _audit_redaction(user, sse.red.matched, path, sse.red.hits)
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
            finally:
                await sem.__aexit__(None, None, None)
        return StreamingResponse(stream_gen(), media_type="text/event-stream")

    try:
        async with admission.hold():
            r = await do_request()
    except Exception as e:
        monitor_end(rid, 502, duration=time.time() - t0)
        return JSONResponse({"error": {"message": f"llama-server request failed: {e}",
                                       "type": "upstream_error"}}, status_code=502)
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
    return _guarded_json_response(r, user, False, path)
