"""One drain and one accounting for every lane stream in the agent loop.

routes/agent/run.py used to carry four near-verbatim copies of the stream+redact+account
block: the greeting short-circuit, the normal step, the executor->main escalation replay,
and the post-stop wrap-up. The escalation copy had already diverged from the normal-step
copy in two places nobody noticed (a chars//4 fallback and a pcached fabrication) - the
standard fate of a cloned block. There is one copy now.

Two deliberate non-uniformities are transcribed, not unified:
- on a cloud->local fallback the escalation copy does NOT adopt the local model info for
  later events, while the greeting and normal-step copies do;
- the wrap-up pass (#4) is a smaller shape (deltas only, no tool accounting) and keeps its
  own inline loop; only copies 1-3 go through drain_llm_stream.

Moved here from routes/agent/guards.py (re-exported there, so existing importers keep
working): guard_flush_events, guard_audit.
"""

import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from ..context_budget import record_usage
from ..db.usage import db_record_request
from ..monitor import _monitor_state, monitor_end, parse_cache_tokens
from ..output_guard import OutputRedactor
from ..sse import sse


def guard_flush_events(redactor, streamed_content) -> list:
    """Flush the output-guard holdback at end of a lane stream. Returns SSE
    chunks the caller must yield (tail delta + one-time guard notice)."""
    chunks = []
    _tail = redactor.flush()
    if _tail:
        streamed_content.append(_tail)
        chunks.append(f"event: delta\ndata: {json.dumps({'text': _tail})}\n\n")
    if redactor.matched:
        chunks.append(f"event: guard\ndata: "
                      + json.dumps({"rule": redactor.matched.get("name"),
                                    "message": redactor.matched.get("message")}) + "\n\n")
    return chunks


def guard_audit(redactor, user, endpoint: str) -> None:
    if redactor.matched:
        from ..audit import audit_log
        audit_log(user, action="output_guard.redact", resource=redactor.matched.get("name"),
                  detail={"endpoint": endpoint, "scope": redactor.matched.get("scope"),
                          "hits": redactor.hits}, result="deny")


@dataclass
class StreamSpec:
    """How one lane stream is drained. One instance per call site."""
    step: int                                # 1-based, for thought/tool event payloads
    model_display: str                       # model_info["display"] at drain start
    audit_label: str                         # e.g. "agent/direct", "agent/executor"
    collect: list = field(default_factory=list)  # safe content chunks accumulate here
    forward_preparing: bool = True           # greeting has no tool_preparing arm
    adopt_fallback_model: bool = True        # escalation keeps the original model_info
    fallback_payload: Optional[dict] = None  # lane event payload on cloud->local fallback


@dataclass
class DrainResult:
    """What the caller needs after the drain. Passed in empty, read after the
    async-for completes (an async generator cannot return a value to `async for`,
    so the result travels out-of-band). model_info is the dict the caller must use
    from here on (original, or the fallback payload when adopted)."""
    res_dict: Optional[dict] = None
    fell_back: bool = False
    model_info: Optional[dict] = None
    # the redactor that processed this stream, for post-loop code that resets it
    # (the semantic-filter and answer-synthesis paths clear its holdback before
    # emitting replacement text)
    redactor: Any = None


async def drain_llm_stream(stream, user, spec: StreamSpec, model_info: dict,
                           is_cloud: bool, out: DrainResult):
    """Drain one lane stream into SSE event strings, yielding each chunk as produced.

    The redactor is created here (not passed in) so the fallback reset cannot drift:
    every copy constructed it the same way and reset it the same way.
    """
    redactor = OutputRedactor(user, is_cloud)
    out.redactor = redactor
    out.model_info = model_info
    async for ev, val in stream:
        if ev == "queued":
            yield f"event: queued\ndata: {json.dumps(val)}\n\n"
            continue
        if ev == "fallback":
            yield f"event: lane\ndata: {json.dumps(spec.fallback_payload)}\n\n"
            redactor = OutputRedactor(user, False)   # lane now local
            out.redactor = redactor
            out.fell_back = True
            if spec.adopt_fallback_model and spec.fallback_payload:
                out.model_info = spec.fallback_payload
            continue
        if ev == "thought_delta":
            yield f"event: thought_delta\ndata: {json.dumps({'step': spec.step, 'delta': val, 'model': out.model_info['display']})}\n\n"
        elif ev == "content_to_thought":
            # forced-open <think>: the text streamed as the answer was reasoning
            redactor.reset()
            spec.collect.clear()
            yield f"event: delta_to_thought\ndata: {json.dumps({'step': spec.step, 'model': out.model_info['display']})}\n\n"
        elif ev == "content_delta":
            _safe = redactor.feed(val)
            spec.collect.append(_safe)
            if _safe:
                yield f"event: delta\ndata: {json.dumps({'text': _safe})}\n\n"
        elif ev == "tool_preparing":
            if spec.forward_preparing:
                yield sse("tool_preparing", {'step': spec.step, **val})
        elif ev == "result":
            out.res_dict = val
    for _c in guard_flush_events(redactor, spec.collect):
        yield _c
    guard_audit(redactor, user, spec.audit_label)


@dataclass
class AccountResult:
    ptoks: Optional[int] = None
    ctoks: Optional[int] = None
    pcached: int = 0
    ccached: int = 0
    dt: Optional[float] = None


def account_llm_result(*, rid: int, res_dict: Optional[dict], streamed: list,
                       msgs: list, model_info: dict, endpoint: Optional[str],
                       usage_lane: Optional[str] = None, usage_tools=None,
                       sent_tokens: int = 0, is_orchestrator: bool = False,
                       run_id: Optional[str] = None) -> AccountResult:
    """Monitor + usage accounting for one drained lane stream.

    endpoint is the db_record_request label (e.g. "agent/executor"). None means the
    greeting shape: completion-only monitor_end, no usage row. Everything else shares
    one path: token anchoring for the context budget, the cache-hit parse, monitor_end,
    and the usage row.

    streamed is the collected content list, or None when the caller never counted chunks
    (the greeting used gen_tokens-or-None, never a length).
    """
    req_mon = _monitor_state["active"].get(rid)
    toks = req_mon.get("gen_tokens") if req_mon else (len(streamed) if streamed is not None else None)
    dt = (time.time() - req_mon["start"]) if req_mon else None
    tps = (toks / dt) if (toks and dt and dt > 0) else None
    if endpoint is None:
        monitor_end(rid, 200, completion_tokens=toks, tps=tps, duration=dt,
                    source=model_info.get("source"), provider=model_info.get("provider_name"))
        return AccountResult(ctoks=toks)
    u = (res_dict or {}).get("usage") or {}
    tim = (res_dict or {}).get("timings") or {}
    pcached, ccached = parse_cache_tokens(u, tim)
    ptoks = u.get("prompt_tokens") or (sum(len(m.get("content", "")) for m in msgs) // 4)
    # Feed the server's exact prompt count back into the budget accounting. Only a real
    # count from the server may anchor the next step - the chars//4 fallback above is
    # itself a guess.
    if u.get("prompt_tokens"):
        record_usage(usage_lane, sent_tokens, u["prompt_tokens"], msgs, usage_tools)
    # never invent a cache figure for a provider that did not report one (it read as a 90%+ hit rate)
    if not pcached and len(msgs) > 1 and model_info.get("source") != "cloud":
        pcached = sum(len(m.get("content", "")) for m in msgs[:-1]) // 4
    ctoks = u.get("completion_tokens") or toks
    monitor_end(rid, 200, prompt_tokens=ptoks, completion_tokens=ctoks, tps=tps, duration=dt,
                model=model_info.get("model"), prompt_cached=pcached, completion_cached=ccached,
                source=model_info.get("source"), provider=model_info.get("provider_name"))
    db_record_request(endpoint, model_info.get("model"), ptoks, ctoks, tps, dt, None, True, 200,
                      prompt_cached_tokens=pcached, completion_cached_tokens=ccached,
                      is_orchestrator=is_orchestrator,
                      source=model_info.get("source"), provider=model_info.get("provider_name"),
                      run_id=run_id)
    return AccountResult(ptoks=ptoks, ctoks=ctoks, pcached=pcached, ccached=ccached, dt=dt)
