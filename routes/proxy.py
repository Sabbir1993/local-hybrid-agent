"""
routes/proxy.py - OpenAI-compatible reverse proxy forwarding requests to the active llama-server.
"""

import asyncio
import copy
import json
import sys
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse, JSONResponse

from core.state import state
from core.auth import Principal
from core.deps import get_current_user
from core.monitor import (
    _monitor_state,
    monitor_begin,
    monitor_end,
    extract_usage_from_stream,
    parse_cache_tokens,
)
from core.db import db_record_request
from core import cloud
from core import input_guard
from core import output_guard
from core.audit import audit_log

router = APIRouter(tags=["proxy"])

_CHAT_PATHS = ("v1/chat/completions", "chat/completions")

# Never forward the caller's app credentials to llama-server / cloud upstreams.
_STRIP_HEADERS = {"host", "cookie", "authorization", "x-csrf-token", "content-length"}


def _upstream_headers(request: Request) -> dict:
    return {k: v for k, v in request.headers.items() if k.lower() not in _STRIP_HEADERS}


def _prompt_texts(payload: dict) -> list:
    """User-authored text in an OpenAI chat / completions / llama.cpp body."""
    texts = []
    for m in payload.get("messages") or []:
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            texts.append(c)
        elif isinstance(c, list):
            texts.extend(str(part.get("text", "")) for part in c
                         if isinstance(part, dict) and part.get("type") == "text")
    prompt = payload.get("prompt")
    if isinstance(prompt, str):
        texts.append(prompt)
    elif isinstance(prompt, list):
        texts.extend(p for p in prompt if isinstance(p, str))
    return texts


def _redact_json(data, user, is_cloud: bool):
    """Redact assistant text in a non-streaming response body (in place)."""
    matched = None
    if not isinstance(data, dict):
        return data, None
    for ch in data.get("choices") or []:
        if not isinstance(ch, dict):
            continue
        msg = ch.get("message")
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            msg["content"], m = output_guard.redact_full(msg["content"], user, is_cloud)
            matched = matched or m
        if isinstance(ch.get("text"), str):
            ch["text"], m = output_guard.redact_full(ch["text"], user, is_cloud)
            matched = matched or m
    if isinstance(data.get("content"), str):   # llama.cpp native /completion
        data["content"], m = output_guard.redact_full(data["content"], user, is_cloud)
        matched = matched or m
    return data, matched


def _audit_redaction(user, matched, path: str, hits: int = 1) -> None:
    if matched:
        audit_log(user, action="output_guard.redact", resource=matched.get("name"),
                  detail={"endpoint": f"proxy/{path}", "scope": matched.get("scope"),
                          "hits": hits}, result="deny")


def _guarded_json_response(r, user, is_cloud: bool, path: str) -> JSONResponse:
    if not r.headers.get("content-type", "").startswith("application/json"):
        text, matched = output_guard.redact_full(r.text, user, is_cloud)
        _audit_redaction(user, matched, path)
        return JSONResponse(content={"raw": text}, status_code=r.status_code)
    data, matched = _redact_json(r.json(), user, is_cloud)
    _audit_redaction(user, matched, path)
    return JSONResponse(content=data, status_code=r.status_code)


class _SSERedactor:
    """Rewrites an SSE byte stream so assistant text passes through the output
    guard. Lines are re-assembled across chunk boundaries; non-data lines and
    non-text fields pass through untouched. Held-back text is flushed as one
    extra delta right before [DONE]."""

    def __init__(self, user, is_cloud: bool):
        self.red = output_guard.OutputRedactor(user, is_cloud)
        self.pending = b""
        self.last_obj = None

    @property
    def active(self) -> bool:
        return bool(self.red.rules)

    def feed(self, chunk: bytes) -> bytes:
        self.pending += chunk
        *lines, self.pending = self.pending.split(b"\n")
        if not lines:
            return b""
        return b"\n".join(self._rewrite(ln.rstrip(b"\r")) for ln in lines) + b"\n"

    def close(self) -> bytes:
        out = self._rewrite(self.pending) if self.pending else b""
        self.pending = b""
        tail = self.red.flush()     # stream ended without [DONE]
        if tail and self.last_obj is not None:
            out += b"\ndata: " + json.dumps(self._as_delta(tail)).encode() + b"\n\n"
        return out

    def _rewrite(self, line: bytes) -> bytes:
        if not line.startswith(b"data:"):
            return line
        body = line[5:].strip()
        if body == b"[DONE]":
            tail = self.red.flush()
            if tail and self.last_obj is not None:
                return b"data: " + json.dumps(self._as_delta(tail)).encode() + b"\n\ndata: [DONE]"
            return line
        try:
            obj = json.loads(body)
        except Exception:
            return line
        if not isinstance(obj, dict):
            return line
        changed = False
        for ch in obj.get("choices") or []:
            if not isinstance(ch, dict):
                continue
            d = ch.get("delta")
            if isinstance(d, dict) and isinstance(d.get("content"), str):
                d["content"] = self.red.feed(d["content"])
                changed = True
            if isinstance(ch.get("text"), str):
                ch["text"] = self.red.feed(ch["text"])
                changed = True
        if isinstance(obj.get("content"), str):
            obj["content"] = self.red.feed(obj["content"])
            changed = True
        self.last_obj = obj
        return b"data: " + json.dumps(obj).encode() if changed else line

    def _as_delta(self, text: str) -> dict:
        o = copy.deepcopy(self.last_obj)
        o.pop("usage", None)
        o.pop("timings", None)
        if o.get("choices"):
            ch = o["choices"][0]
            if "delta" in ch:
                ch["delta"] = {"content": text}
            if "text" in ch:
                ch["text"] = text
            ch["finish_reason"] = None
            o["choices"] = [ch]
        if "content" in o:
            o["content"] = text
        return o


async def _proxy_cloud(cm, path: str, request: Request, body: bytes, user=None):
    """Forward a chat-completions request straight to a cloud-bound main lane.

    Raises on failure so the caller can decide whether to fall back to the
    local lane (per the user's cloud.fallback_local binding).
    """
    client = cloud.CloudClient(cm)
    payload = json.loads(body) if body else {}
    is_streaming = bool(payload.get("stream"))
    t0 = time.time()
    state.last_activity = time.time()
    rid = monitor_begin(path, is_streaming, body, model=cm.model_id, source="cloud", provider=cm.provider_name)

    try:
        return await _proxy_cloud_inner(client, cm, path, payload, is_streaming, rid, t0, user)
    except Exception:
        monitor_end(rid, 0, duration=time.time() - t0)
        raise


async def _proxy_cloud_inner(client, cm, path, payload, is_streaming, rid, t0, user=None):
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
            sse = _SSERedactor(user, True)
            try:
                async for chunk in r0.aiter_bytes():
                    u = extract_usage_from_stream(chunk.decode("utf-8", "replace"), rid)
                    if u:
                        usage = u
                    yield sse.feed(chunk) if sse.active else chunk
                if sse.active:
                    yield sse.close()
            finally:
                await probe.__aexit__(None, None, None)
            _audit_redaction(user, sse.red.matched, path, sse.red.hits)
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
    return _guarded_json_response(r, user, True, path)


@router.api_route("/{path:path}", methods=["GET", "POST"])
async def proxy(path: str, request: Request, user: Principal = Depends(get_current_user)):
    if path in ("favicon.ico", "index.html"):
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
        return StreamingResponse(stream_gen(), media_type="text/event-stream")

    try:
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


