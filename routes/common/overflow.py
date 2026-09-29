import json
import sys
from typing import Optional
from core.agent_loop import compact_messages

from .sse_stream import _process_sse_stream


def _is_context_overflow(status_code, err_text) -> bool:
    """True when this refusal is "your prompt is bigger than my context window".

    Checked by body text, not just status: llama-server answers 400 with
    `exceed_context_size_error`, and some gateways keep a different status. Every
    attempt of a request - the first one and each retry - must be tested, because a
    retry can be the one that crosses the limit."""
    try:
        if int(status_code or 0) != 400:
            return False
    except (TypeError, ValueError):
        return False
    body = bytes(err_text or b"").decode("utf-8", "replace")
    return "exceed_context_size_error" in body or "exceeds the available context size" in body


async def _recover_context(client_or_state, payload: dict, msgs: list, tools,
                          rid: Optional[int], err_text):
    """Re-send an oversized request after compaction: pruned messages (window taken
    from the refusal body), then - if even that is refused - the same pruned prompt
    without the tool schemas. Yields the successful response's items, or raises a
    RuntimeError tagged '(after context compaction)' so a genuine second failure is
    still diagnosable. Never re-enters _llm_chat_stream_raw, so it cannot loop."""
    emergency_msgs = _emergency_compact(msgs, tools, _ctx_from_error(err_text))
    payload_emergency = dict(payload)
    payload_emergency["messages"] = emergency_msgs
    async with client_or_state.stream("POST", "/v1/chat/completions", json=payload_emergency, timeout=None) as em_resp:
        if em_resp.status_code == 200:
            async for item in _process_sse_stream(em_resp, rid=rid):
                yield item
            return
        em_code = em_resp.status_code
        em_err = await em_resp.aread()
    if tools:
        print("[common] context still exceeded after compaction - retrying without tools", file=sys.stderr)
        payload_notools = dict(payload_emergency)
        payload_notools.pop("tools", None)
        async with client_or_state.stream("POST", "/v1/chat/completions", json=payload_notools, timeout=None) as nt_resp:
            if nt_resp.status_code == 200:
                async for item in _process_sse_stream(nt_resp, rid=rid):
                    yield item
                return
            nt_code = nt_resp.status_code
            nt_err = await nt_resp.aread()
        raise RuntimeError(f"upstream {nt_code} (after context compaction, tools removed): {nt_err.decode('utf-8', 'replace')[:200]}")
    raise RuntimeError(f"upstream {em_code} (after context compaction): {em_err.decode('utf-8', 'replace')[:200]}")


def _ctx_from_error(err_bytes) -> int:
    """The window llama-server enforces, read out of its overflow body
    (`{"error": {..., "n_ctx": 32768}}`). Authoritative at exactly the moment we
    need it, so it beats any local estimate; 0 when the body is not that shape."""
    try:
        j = json.loads(bytes(err_bytes or b"").decode("utf-8", "replace"))
    except Exception:
        return 0
    if not isinstance(j, dict):
        return 0
    for src in (j.get("error"), j):
        if isinstance(src, dict):
            try:
                n = int(src.get("n_ctx") or 0)
            except (TypeError, ValueError):
                n = 0
            if n > 0:
                return n
    return 0


def _emergency_compact(msgs: list, tools, ctx: int) -> list:
    """Last-resort prompt for a request the server already refused.

    With a known window this reuses the loop's own compaction (tool-aware, keeps
    the system prompt + newest turn) at a conservative budget. Without one it
    falls back to the historic fixed shape: system prompt + last 3 messages with
    oversized contents halved. Never returns an empty list for non-empty input."""
    system = [msgs[0]] if msgs and msgs[0].get("role") == "system" else []
    tail = list(msgs[1:] if system else msgs)
    if ctx and ctx > 2000 and (len(system) + len(tail)) >= 2:
        try:
            compacted = compact_messages(system + tail, max(1000, int(ctx * 0.6)), tools=tools)
            if len(compacted) >= 2 or not tail:
                return compacted
        except Exception:
            pass
    out = list(system)
    for m in (tail[-3:] if len(tail) > 3 else tail):
        m_copy = dict(m)
        c = str(m_copy.get("content") or "")
        if len(c) > 2000:
            m_copy["content"] = c[:1000] + "\n...[truncated to fit context budget]...\n" + c[-1000:]
        out.append(m_copy)
    return out
