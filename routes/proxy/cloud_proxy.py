"""Cloud proxy forwarding logic."""

import json
import time

from fastapi.responses import StreamingResponse

from core import cloud
from core.db import db_record_request
from core.monitor import extract_usage_from_stream, monitor_begin, monitor_end
from core.state import state
from .guard import _SSERedactor, _audit_redaction, _guarded_json_response


async def _proxy_cloud(cm, path: str, request, body: bytes, user=None):
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
