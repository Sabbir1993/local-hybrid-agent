"""
routes/common/llm_stream.py - LLM streaming helpers: slot fields, raw stream,
gated stream, and fallback stream.
"""

import json
import sys
from typing import Optional

from core import reasoning
from core.agent_loop import safe_parse_and_repair_args
from core.request_context import get_current_user_id
from core.small_model import APP_CONFIG
from core.state import state

from .admission import admission
from .sse_stream import _process_sse_stream


def _main_slot_fields() -> dict:
    """Slot affinity for the local main llama-server: pin each user to one slot
    so their conversation prefix stays cached there between turns (instead of
    landing on whichever slot is free and re-prefilling from token 0)."""
    fields = {"cache_prompt": True}
    n_slots = int((state.profile or {}).get("n_slots") or 1)
    uid = get_current_user_id()
    if n_slots > 1 and uid is not None and (APP_CONFIG.get("serving") or {}).get("slot_affinity", True):
        fields["id_slot"] = int(uid) % n_slots
    return fields


async def _llm_chat_stream_raw(client_or_state, msgs: list, tools=None, temperature=0.4, max_tokens=-1, repeat_penalty=1.15, rid: Optional[int] = None, grammar: Optional[str] = None, extra: Optional[dict] = None, effort: Optional[str] = None, top_p: Optional[float] = None, min_p: Optional[float] = None, presence_penalty: Optional[float] = None, top_k: Optional[int] = None):
    payload = {
        "messages": msgs,
        "temperature": temperature,
        "repeat_penalty": repeat_penalty,
        "stream": True,
    }
    if top_p is not None:
        payload["top_p"] = float(top_p)
    if min_p is not None and client_or_state is state.client:
        payload["min_p"] = float(min_p)
    if presence_penalty is not None:
        payload["presence_penalty"] = float(presence_penalty)
    if top_k is not None and int(top_k) > 0 and client_or_state is state.client:
        payload["top_k"] = int(top_k)
    if client_or_state is state.client:
        payload.update(_main_slot_fields())
        # llama-server-only fields (e.g. chat_template_kwargs); never sent to
        # cloud providers, which may reject unknown keys
        if extra:
            payload.update(extra)
        # reasoning effort (level already resolved by the route); its template
        # kwargs are merged so they don't clobber other chat_template_kwargs
        for k, v in reasoning.local_fields(effort).items():
            if k == "chat_template_kwargs":
                payload[k] = {**(payload.get(k) or {}), **v}
            else:
                payload[k] = v
    elif getattr(client_or_state, "is_cloud", False):
        payload.update(reasoning.cloud_fields(effort, getattr(client_or_state, "cm", None)))
    if max_tokens is not None and int(max_tokens) > 0:
        payload["max_tokens"] = int(max_tokens)
    else:
        payload["max_tokens"] = -1
    if tools:
        payload["tools"] = tools
    if grammar:
        payload["grammar"] = grammar

    async with client_or_state.stream("POST", "/v1/chat/completions", json=payload, timeout=None) as response:
        if response.status_code != 200:
            err_text = await response.aread()
            err_msg = err_text.decode("utf-8", "replace")[:300]
            effort_keys = [k for k in reasoning.CLOUD_KEYS if k in payload]
            if effort_keys and response.status_code in (400, 422) and getattr(client_or_state, "is_cloud", False):
                # Provider/build rejected the reasoning fields: retry once
                # without them (model's default thinking) rather than failing.
                print(f"[common] reasoning fields {effort_keys} rejected ({response.status_code}: {err_msg[:120]}) - retrying without them", file=sys.stderr)
                async for item in _llm_chat_stream_raw(client_or_state, msgs, tools, temperature, max_tokens,
                                                       repeat_penalty, rid, grammar, extra=extra):
                    yield item
                return
            if grammar:
                # This llama-server build rejected the grammar (unsupported field
                # or invalid GBNF). Retry once unconstrained so the lane survives.
                print(f"[common] grammar-constrained request rejected ({response.status_code}: {err_msg[:120]}) - retrying without grammar", file=sys.stderr)
                payload.pop("grammar", None)
                async with client_or_state.stream("POST", "/v1/chat/completions", json=payload, timeout=None) as rg:
                    if rg.status_code != 200:
                        rg_err = await rg.aread()
                        raise RuntimeError(f"upstream {rg.status_code}: {rg_err.decode('utf-8', 'replace')[:200]}")
                    async for item in _process_sse_stream(rg, rid=rid):
                        yield item
                return
            if response.status_code == 500 and "Failed to parse tool call arguments as JSON" in err_msg:
                sanitized_msgs = []
                for m in msgs:
                    m_copy = dict(m)
                    if m_copy.get("tool_calls"):
                        new_tcs = []
                        for tc in m_copy["tool_calls"]:
                            tc_c = dict(tc)
                            fn_c = dict(tc_c.get("function", {}))
                            raw_a = fn_c.get("arguments", "{}")
                            if isinstance(raw_a, str):
                                try:
                                    json.loads(raw_a)
                                except Exception:
                                    fn_c["arguments"] = json.dumps(safe_parse_and_repair_args(raw_a))
                            else:
                                fn_c["arguments"] = json.dumps(raw_a)
                            tc_c["function"] = fn_c
                            new_tcs.append(tc_c)
                        m_copy["tool_calls"] = new_tcs
                    sanitized_msgs.append(m_copy)
                payload["messages"] = sanitized_msgs
                async with client_or_state.stream("POST", "/v1/chat/completions", json=payload, timeout=None) as retry_resp:
                    if retry_resp.status_code != 200:
                        re_err = await retry_resp.aread()
                        payload_notools = dict(payload)
                        payload_notools.pop("tools", None)
                        try:
                            async with client_or_state.stream("POST", "/v1/chat/completions", json=payload_notools, timeout=None) as fb_resp:
                                if fb_resp.status_code == 200:
                                    async for item in _process_sse_stream(fb_resp, rid=rid):
                                        yield item
                                    return
                        except Exception:
                            pass
                        raise RuntimeError(f"upstream {retry_resp.status_code}: {re_err.decode('utf-8', 'replace')[:200]}")
                    async for item in _process_sse_stream(retry_resp, rid=rid):
                        yield item
                return
            raise RuntimeError(f"upstream {response.status_code}: {err_msg}")

        async for item in _process_sse_stream(response, rid=rid):
            yield item


async def _llm_chat_stream(client_or_state, msgs: list, tools=None, temperature=0.4, max_tokens=-1, repeat_penalty=1.15, rid: Optional[int] = None, grammar: Optional[str] = None, extra: Optional[dict] = None, effort: Optional[str] = None, top_p: Optional[float] = None, min_p: Optional[float] = None, presence_penalty: Optional[float] = None, top_k: Optional[int] = None):
    """Stream one completion. Requests to the local main llama-server first pass
    the fair-share admission gate; a ("queued", {"position": n}) item is yielded
    when the caller has to wait for a slot."""
    if client_or_state is not state.client or not admission.enabled():
        async for item in _llm_chat_stream_raw(client_or_state, msgs, tools, temperature, max_tokens,
                                               repeat_penalty, rid, grammar, extra=extra, effort=effort,
                                               top_p=top_p, min_p=min_p, presence_penalty=presence_penalty, top_k=top_k):
            yield item
        return
    glob, mine = admission._sems(get_current_user_id())
    if glob.locked() or mine.locked():
        yield ("queued", {"position": admission.waiting + 1})
    admission.waiting += 1
    try:
        await mine.acquire()
        try:
            await glob.acquire()
        except BaseException:
            mine.release()
            raise
    finally:
        admission.waiting -= 1
    try:
        async for item in _llm_chat_stream_raw(client_or_state, msgs, tools, temperature, max_tokens,
                                               repeat_penalty, rid, grammar, extra=extra, effort=effort,
                                               top_p=top_p, min_p=min_p, presence_penalty=presence_penalty, top_k=top_k):
            yield item
    finally:
        glob.release()
        mine.release()


async def _llm_chat_stream_with_fallback(primary, fallback, msgs: list, tools=None, temperature=0.4,
                                        max_tokens=-1, repeat_penalty=1.15, rid: Optional[int] = None,
                                        grammar: Optional[str] = None, lane: str = "cloud",
                                        extra: Optional[dict] = None, effort: Optional[str] = None,
                                        top_p: Optional[float] = None, min_p: Optional[float] = None,
                                        presence_penalty: Optional[float] = None, top_k: Optional[int] = None):
    """Stream from `primary` (usually a CloudClient); if it fails before emitting any
    content, retry the same request on `fallback` (the local lane) instead of failing
    the whole agent step. Yields ("fallback", reason) once before switching so callers
    can tell the UI which engine actually answered."""
    produced = False
    try:
        async for item in _llm_chat_stream(primary, msgs, tools, temperature, max_tokens,
                                           repeat_penalty, rid, grammar, extra=extra, effort=effort,
                                           top_p=top_p, min_p=min_p, presence_penalty=presence_penalty, top_k=top_k):
            produced = True
            yield item
        return
    except Exception as e:
        if produced or fallback is None:
            raise
        reason = f"{type(e).__name__}: {str(e)[:200]}"
        print(f"[common] {lane} cloud lane failed ({reason}) - falling back to the local model",
              file=sys.stderr)
        yield ("fallback", reason)
    async for item in _llm_chat_stream(fallback, msgs, tools, temperature, max_tokens,
                                       repeat_penalty, rid, grammar, extra=extra, effort=effort,
                                       top_p=top_p, min_p=min_p, presence_penalty=presence_penalty, top_k=top_k):
        yield item
