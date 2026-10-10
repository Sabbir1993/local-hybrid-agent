import asyncio
import json
import sys
import time
from contextlib import asynccontextmanager
from typing import Optional
from core import reasoning
from core.agent_loop import safe_parse_and_repair_args
from core.limits import cloud_gate
from core.llm_messages import normalize_system_messages
from core.request_context import get_current_user_id
from core.small_model import APP_CONFIG
from core.state import state

from .overflow import _is_context_overflow, _recover_context
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
        self._users_seen: dict = {}          # uid -> last request time, for pruning
        self._users_n = 0
        self.waiting = 0

    # a per-user semaphore is tiny, but _users was never pruned: one entry per user id that
    # ever made a request, kept for the life of the process. Pruned on access, so an idle app
    # never grows and a busy one cannot outrun its own cleanup.
    _USER_IDLE_S = 3600.0

    def _cfg(self) -> dict:
        return APP_CONFIG.get("serving") or {}

    def enabled(self) -> bool:
        return bool(self._cfg().get("queue_enabled", True))

    def _prune_users(self, now: float) -> None:
        stale = [uid for uid, seen in self._users_seen.items()
                 if now - seen > self._USER_IDLE_S and not self._users[uid].locked()]
        for uid in stale:
            if not self._users[uid].locked() and not self._users[uid]._waiters:
                self._users.pop(uid, None)
                self._users_seen.pop(uid, None)

    def _sems(self, uid):
        n = max(1, int((state.profile or {}).get("n_slots") or 1))
        if self._global is None or n != self._global_n:
            # resized on model (re)load; holders of the old one release it harmlessly
            self._global, self._global_n = asyncio.Semaphore(n), n
        per_user = max(1, int(self._cfg().get("max_inflight_per_user", 1)))
        if per_user != self._users_n:
            self._users, self._users_n, self._users_seen = {}, per_user, {}
        now = time.time()
        self._prune_users(now)
        us = self._users.get(uid)
        if us is None:
            us = self._users[uid] = asyncio.Semaphore(per_user)
        self._users_seen[uid] = now
        return self._global, us

    @asynccontextmanager
    async def hold(self):
        """Hold both semaphores for the length of a block.

        For callers that forward raw bytes instead of going through _llm_chat_stream -- the
        OpenAI-compatible /v1/* proxy did exactly that, so it bypassed the gate completely and
        one user could occupy every llama-server slot through the raw API. There is no SSE
        channel here to report a queue position on, so a wait is simply a wait."""
        if not self.enabled():
            yield
            return
        glob, mine = self._sems(get_current_user_id())
        await mine.acquire()
        try:
            await glob.acquire()
        except BaseException:
            mine.release()
            raise
        try:
            yield
        finally:
            glob.release()
            mine.release()


admission = _Admission()


async def _llm_chat_stream(client_or_state, msgs: list, tools=None, temperature=0.4, max_tokens=-1, repeat_penalty=1.15, rid: Optional[int] = None, grammar: Optional[str] = None, extra: Optional[dict] = None, effort: Optional[str] = None, top_p: Optional[float] = None, min_p: Optional[float] = None, presence_penalty: Optional[float] = None, top_k: Optional[int] = None, tool_choice: Optional[str] = None):
    """Stream one completion. Requests to the local main llama-server first pass
    the fair-share admission gate; a ("queued", {"position": n}) item is yielded
    when the caller has to wait for a slot.

    Cloud lanes pass a cloud concurrency gate instead (core/limits.py). They used to pass
    through unthrottled entirely, and cloud calls cost money."""
    if client_or_state is not state.client:
        if cloud_gate.enabled():
            await cloud_gate.sem().acquire()
            try:
                async for item in _llm_chat_stream_raw(client_or_state, msgs, tools, temperature, max_tokens,
                                                       repeat_penalty, rid, grammar, extra=extra, effort=effort,
                                                       top_p=top_p, min_p=min_p, presence_penalty=presence_penalty,
                                                       top_k=top_k, tool_choice=tool_choice):
                    yield item
            finally:
                cloud_gate.sem().release()
        else:
            async for item in _llm_chat_stream_raw(client_or_state, msgs, tools, temperature, max_tokens,
                                                   repeat_penalty, rid, grammar, extra=extra, effort=effort,
                                                   top_p=top_p, min_p=min_p, presence_penalty=presence_penalty,
                                                   top_k=top_k, tool_choice=tool_choice):
                yield item
        return
    if not admission.enabled():
        async for item in _llm_chat_stream_raw(client_or_state, msgs, tools, temperature, max_tokens,
                                               repeat_penalty, rid, grammar, extra=extra, effort=effort,
                                               top_p=top_p, min_p=min_p, presence_penalty=presence_penalty, top_k=top_k,
                                               tool_choice=tool_choice):
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
                                               top_p=top_p, min_p=min_p, presence_penalty=presence_penalty, top_k=top_k,
                                               tool_choice=tool_choice):
            yield item
    finally:
        glob.release()
        mine.release()


# clients (by base URL / cloud model key) that rejected a tool_choice field
_TOOL_CHOICE_UNSUPPORTED: set = set()


def _tool_choice_key(client_or_state) -> str:
    cm = getattr(client_or_state, "cm", None)
    if cm is not None:
        return f"cloud:{cm.key}"
    return f"local:{getattr(client_or_state, 'base_url', '') or id(client_or_state)}"


async def _llm_chat_stream_raw(client_or_state, msgs: list, tools=None, temperature=0.4, max_tokens=-1, repeat_penalty=1.15, rid: Optional[int] = None, grammar: Optional[str] = None, extra: Optional[dict] = None, effort: Optional[str] = None, top_p: Optional[float] = None, min_p: Optional[float] = None, presence_penalty: Optional[float] = None, top_k: Optional[int] = None, tool_choice: Optional[str] = None):
    msgs = normalize_system_messages(msgs)   # a system message mid-conversation 500s on Qwen/Llama templates (also the retry paths below)
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
    else:
        # helper lanes (executor, vision): reuse the KV cache for the stable prompt prefix
        # (system prompt + tools) between steps - the request is otherwise reprocessed each step
        payload["cache_prompt"] = True
        # Only an explicit "none" reaches a helper lane: the executor otherwise thinks on the template default with
        # no budget (a spoken turn cannot wait for that). Other levels stay main-lane-only, as before.
        if effort == "none":
            payload.update(reasoning.local_fields("none"))
    if max_tokens is not None and int(max_tokens) > 0:
        payload["max_tokens"] = int(max_tokens)
    else:
        payload["max_tokens"] = -1
    if tools:
        payload["tools"] = tools
        # "required" makes a tool call the only possible reply. Servers/providers that
        # rejected it once are remembered and not asked again (see the retry below).
        if tool_choice and _tool_choice_key(client_or_state) not in _TOOL_CHOICE_UNSUPPORTED:
            payload["tool_choice"] = tool_choice
    if grammar:
        payload["grammar"] = grammar

    async with client_or_state.stream("POST", "/v1/chat/completions", json=payload, timeout=None) as response:
        if response.status_code != 200:
            err_text = await response.aread()
            err_msg = err_text.decode("utf-8", "replace")[:300]
            effort_keys = [k for k in reasoning.CLOUD_KEYS if k in payload]
            # Context overflow is handled FIRST, ahead of the reasoning / grammar /
            # JSON-repair retries below: each of those raises on its own non-200 and
            # would otherwise swallow the only chance to compact and recover. This is
            # the branch that used to sit last, which made it unreachable whenever a
            # grammar was set - the executor lane's default.
            if _is_context_overflow(response.status_code, err_text):
                print(f"[common] context size exceeded upstream ({err_msg[:120]}) - auto-compacting and retrying", file=sys.stderr)
                async for item in _recover_context(client_or_state, payload, msgs, tools, rid, err_text):
                    yield item
                return
            if "tool_choice" in payload and (response.status_code in (400, 422)
                                             or (response.status_code == 500 and "tool_choice" in err_msg.lower())):
                # this server/provider does not accept tool_choice: remember it and
                # retry the same request once without it
                _TOOL_CHOICE_UNSUPPORTED.add(_tool_choice_key(client_or_state))
                print(f"[common] tool_choice rejected ({response.status_code}: {err_msg[:120]}) - retrying without it",
                      file=sys.stderr)
                async for item in _llm_chat_stream_raw(client_or_state, msgs, tools, temperature, max_tokens,
                                                       repeat_penalty, rid, grammar, extra=extra, effort=effort,
                                                       top_p=top_p, min_p=min_p, presence_penalty=presence_penalty,
                                                       top_k=top_k):
                    yield item
                return
            if effort_keys and response.status_code in (400, 422) and getattr(client_or_state, "is_cloud", False):
                # Provider/build rejected the reasoning fields: retry once
                # without them (model's default thinking) rather than failing.
                print(f"[common] reasoning fields {effort_keys} rejected ({response.status_code}: {err_msg[:120]}) - retrying without them", file=sys.stderr)
                async for item in _llm_chat_stream_raw(client_or_state, msgs, tools, temperature, max_tokens,
                                                       repeat_penalty, rid, grammar, extra=extra, tool_choice=tool_choice):
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
                        # The unconstrained retry can be the attempt that crosses the
                        # context limit (the first one may have been refused for the
                        # grammar alone) - recover instead of surfacing the 400.
                        if _is_context_overflow(rg.status_code, rg_err):
                            print(f"[common] context size exceeded on the grammar retry "
                                  f"({rg_err.decode('utf-8', 'replace')[:120]}) - compacting", file=sys.stderr)
                            async for item in _recover_context(client_or_state, payload, msgs, tools, rid, rg_err):
                                yield item
                            return
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
                        # Same hole as the grammar retry: this sanitized attempt can be
                        # the one that crosses the context limit.
                        if _is_context_overflow(retry_resp.status_code, re_err):
                            print(f"[common] context size exceeded on the tool-call repair retry "
                                  f"({re_err.decode('utf-8', 'replace')[:120]}) - compacting", file=sys.stderr)
                            async for item in _recover_context(client_or_state, payload, msgs, tools, rid, re_err):
                                yield item
                            return
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
