"""
routes/common.py - Shared state and LLM streaming utilities across routes.
"""

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.monitor import monitor_token
from core.agent_loop import safe_parse_and_repair_args
from core.request_context import get_current_user_id
from core.small_model import APP_CONFIG
from core.state import state

# Global CLI runtime overrides
initial_profile_path: Optional[Path] = None
models_dir: Optional[Path] = None
curStatus_model_hint: Optional[str] = None

def current_date_prompt() -> str:
    """System-prompt line anchoring "now". Without it a local model assumes its
    training-cutoff year and searches the web for stale years."""
    now = datetime.now().astimezone()
    return (f"CURRENT DATE: {now:%Y-%m-%d} ({now:%A}). Treat this as 'now' - your training data ends "
            f"earlier. For recent or current data, search with {now.year} (and {now.year - 1} for the "
            f"latest full year); never assume your training-cutoff year is the current year.")


def main_ctx_tokens(cloud_main=None) -> int:
    """Context window of one main-lane conversation. llama-server divides -c
    across -np slots unless the KV pool is unified (then --kv-unified-per-slot,
    if set, is the cap)."""
    from core.process import per_slot_cap
    if cloud_main:
        return getattr(cloud_main, "ctx", 32768) or 32768
    p = state.profile if isinstance(state.profile, dict) else {}
    ctx = int(p.get("context_size") or 32768)
    n_slots = int(p.get("n_slots") or 1)
    if p.get("kv_unified"):
        return per_slot_cap(p) or ctx
    return ctx // n_slots if n_slots > 1 else ctx


async def _process_sse_stream(response, rid: Optional[int] = None):
    content_acc = []
    reasoning_acc = []
    accumulated_tcs = {}
    in_think_tag = False

    last_usage = None
    last_timings = None
    finish_reason = None
    async for line in response.aiter_lines():
        line = line.strip()
        if not line or not line.startswith("data: "):
            continue
        raw = line[6:].strip()
        if raw == "[DONE]":
            break
        try:
            data = json.loads(raw)
        except Exception:
            continue

        if isinstance(data.get("usage"), dict):
            last_usage = data["usage"]
        if isinstance(data.get("timings"), dict):
            last_timings = data["timings"]

        choices = data.get("choices") or []
        if not choices:
            continue
        if choices[0].get("finish_reason"):
            finish_reason = choices[0]["finish_reason"]
        delta = choices[0].get("delta") or {}

        r_chunk = delta.get("reasoning_content")
        if r_chunk:
            reasoning_acc.append(r_chunk)
            if rid:
                monitor_token(rid, 1)
            yield ("thought_delta", r_chunk)

        c_chunk = delta.get("content")
        if c_chunk:
            if "<think>" in c_chunk:
                in_think_tag = True
                parts = c_chunk.split("<think>", 1)
                if parts[0]:
                    content_acc.append(parts[0])
                    if rid:
                        monitor_token(rid, 1)
                    yield ("content_delta", parts[0])
                if len(parts) > 1 and parts[1]:
                    if "</think>" in parts[1]:
                        in_think_tag = False
                        th_part, post = parts[1].split("</think>", 1)
                        reasoning_acc.append(th_part)
                        if rid:
                            monitor_token(rid, 1)
                        yield ("thought_delta", th_part)
                        if post:
                            content_acc.append(post)
                            if rid:
                                monitor_token(rid, 1)
                            yield ("content_delta", post)
                    else:
                        reasoning_acc.append(parts[1])
                        if rid:
                            monitor_token(rid, 1)
                        yield ("thought_delta", parts[1])
            elif "</think>" in c_chunk and in_think_tag:
                in_think_tag = False
                th_part, post = c_chunk.split("</think>", 1)
                if th_part:
                    reasoning_acc.append(th_part)
                    if rid:
                        monitor_token(rid, 1)
                    yield ("thought_delta", th_part)
                if post:
                    content_acc.append(post)
                    if rid:
                        monitor_token(rid, 1)
                    yield ("content_delta", post)
            elif in_think_tag:
                reasoning_acc.append(c_chunk)
                if rid:
                    monitor_token(rid, 1)
                yield ("thought_delta", c_chunk)
            else:
                content_acc.append(c_chunk)
                if rid:
                    monitor_token(rid, 1)
                yield ("content_delta", c_chunk)

        tcs = delta.get("tool_calls") or []
        for tc in tcs:
            idx = tc.get("index", 0)
            if idx not in accumulated_tcs:
                accumulated_tcs[idx] = {
                    "id": tc.get("id") or f"call_{idx}",
                    "type": "function",
                    "function": {"name": "", "arguments": ""}
                }
            fn = tc.get("function") or {}
            if fn.get("name"):
                accumulated_tcs[idx]["function"]["name"] += fn["name"]
                if rid:
                    monitor_token(rid, 1)
            if fn.get("arguments"):
                accumulated_tcs[idx]["function"]["arguments"] += fn["arguments"]
                if rid:
                    # Estimate generated token count from character chunks
                    chunk_toks = max(1, len(fn["arguments"]) // 4)
                    monitor_token(rid, chunk_toks)

    tool_calls = [accumulated_tcs[k] for k in sorted(accumulated_tcs.keys())]
    full_content = "".join(content_acc)
    full_reasoning = "".join(reasoning_acc)
    yield ("result", {
        "content": full_content,
        "reasoning": full_reasoning,
        "tool_calls": tool_calls,
        "usage": last_usage,
        "timings": last_timings,
        "finish_reason": finish_reason,
    })




async def _llm_chat_stream_with_fallback(primary, fallback, msgs: list, tools=None, temperature=0.4,
                                        max_tokens=-1, repeat_penalty=1.15, rid: Optional[int] = None,
                                        grammar: Optional[str] = None, lane: str = "cloud",
                                        extra: Optional[dict] = None):
    """Stream from `primary` (usually a CloudClient); if it fails before emitting any
    content, retry the same request on `fallback` (the local lane) instead of failing
    the whole agent step. Yields ("fallback", reason) once before switching so callers
    can tell the UI which engine actually answered."""
    produced = False
    try:
        async for item in _llm_chat_stream(primary, msgs, tools, temperature, max_tokens,
                                           repeat_penalty, rid, grammar, extra=extra):
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
                                       repeat_penalty, rid, grammar, extra=extra):
        yield item


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


class _Admission:
    """Fair-share gate in front of the local main llama-server.

    - global: at most n_slots generations in flight (one per llama-server slot),
      so extra requests wait here -- where the UI can be told -- instead of
      silently inside llama-server;
    - per user: at most `max_inflight_per_user` (default 1), so one person's
      agent loop / second tab can't occupy every slot while others wait.
    Configured under app.json "serving"."""

    def __init__(self):
        self._global: Optional[asyncio.Semaphore] = None
        self._global_n = 0
        self._users: dict = {}
        self._users_n = 0
        self.waiting = 0

    def _cfg(self) -> dict:
        return APP_CONFIG.get("serving") or {}

    def enabled(self) -> bool:
        return bool(self._cfg().get("queue_enabled", True))

    def _sems(self, uid):
        n = max(1, int((state.profile or {}).get("n_slots") or 1))
        if self._global is None or n != self._global_n:
            # resized on model (re)load; holders of the old one release it harmlessly
            self._global, self._global_n = asyncio.Semaphore(n), n
        per_user = max(1, int(self._cfg().get("max_inflight_per_user", 1)))
        if per_user != self._users_n:
            self._users, self._users_n = {}, per_user
        us = self._users.get(uid)
        if us is None:
            us = self._users[uid] = asyncio.Semaphore(per_user)
        return self._global, us


admission = _Admission()


async def _llm_chat_stream(client_or_state, msgs: list, tools=None, temperature=0.4, max_tokens=-1, repeat_penalty=1.15, rid: Optional[int] = None, grammar: Optional[str] = None, extra: Optional[dict] = None):
    """Stream one completion. Requests to the local main llama-server first pass
    the fair-share admission gate; a ("queued", {"position": n}) item is yielded
    when the caller has to wait for a slot."""
    if client_or_state is not state.client or not admission.enabled():
        async for item in _llm_chat_stream_raw(client_or_state, msgs, tools, temperature, max_tokens,
                                               repeat_penalty, rid, grammar, extra=extra):
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
                                               repeat_penalty, rid, grammar, extra=extra):
            yield item
    finally:
        glob.release()
        mine.release()


async def _llm_chat_stream_raw(client_or_state, msgs: list, tools=None, temperature=0.4, max_tokens=-1, repeat_penalty=1.15, rid: Optional[int] = None, grammar: Optional[str] = None, extra: Optional[dict] = None):
    payload = {
        "messages": msgs,
        "temperature": temperature,
        "repeat_penalty": repeat_penalty,
        "stream": True,
    }
    if client_or_state is state.client:
        payload.update(_main_slot_fields())
        # llama-server-only fields (e.g. chat_template_kwargs); never sent to
        # cloud providers, which may reject unknown keys
        if extra:
            payload.update(extra)
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



