"""Prompt-budget accounting for the agent loop.

Two sources of truth replace estimation guesswork:

  * the window comes from the running server (`GET /slots` -> `n_ctx`, the same
    number llama-server reports when it refuses an oversized request), so
    `-np` / `-kvu` / `--kv-unified-per-slot` can never disagree with our math;
    config-derived values (np / -kvu aware) are the fallback when no server is
    reachable or the lane is a cloud provider.
  * the prompt size is anchored on llama-server's own `usage.prompt_tokens` for
    the previous request in that lane, and only the delta since then is
    extrapolated with a learned chars-per-token ratio. With no anchor yet (first
    step, or a compaction shrank the prompt) it falls back to
    `core.agent_loop.estimate_prompt_tokens` times a safety factor clamped to
    [1.0, 2.0] - the factor only ever makes the budget more conservative.

Why an anchor: the estimate is chars//3, which undercounts code/JSON-heavy
prompts, and tool schemas used to be ignored entirely. Anchoring makes the error
a function of one step's delta instead of the whole history, and never compounds.

No LLM calls here. The only I/O is one cached `GET /slots` probe per lane.
"""

import time
from typing import Optional

# Fraction of the window a prompt may occupy before compaction kicks in. The
# remainder covers the completion plus the estimator's residual error.
MARGIN = 0.70
SAFETY_MIN, SAFETY_MAX = 1.0, 2.0
CHARS_PER_TOKEN_DEFAULT = 3.0
CHARS_PER_TOKEN_MIN, CHARS_PER_TOKEN_MAX = 1.0, 8.0
EMA_ALPHA = 0.25              # weight of the newest sample
MIN_DELTA_CHARS = 200         # below this the delta is too small to learn from
PROBE_TTL_S = 120.0           # /slots cache; a model reload re-probes
PROBE_RETRY_S = 60.0          # back off after a failed probe
MIN_WINDOW = 512

# lane -> accounting state. Process-local on purpose: the numbers are only used
# to decide when to compact, never persisted or shown as exact metrics.
_STATE: dict = {}


def _state(lane: str) -> dict:
    st = _STATE.get(lane)
    if st is None:
        st = _STATE[lane] = {
            "window": None, "probed_at": 0.0, "probe_failed_at": 0.0,
            "factor": SAFETY_MIN, "ratio": CHARS_PER_TOKEN_DEFAULT,
            "last_actual": None, "last_chars": None,
        }
    return st


def reset(lane: Optional[str] = None) -> None:
    """Forget learned state (tests; also useful on a model (re)load)."""
    if lane is None:
        _STATE.clear()
    else:
        _STATE.pop(lane, None)


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def prompt_chars(msgs: list, tools: Optional[list] = None) -> int:
    """Size of a request in characters: message contents, tool-call envelopes and
    the tool schemas. This is the quantity anchored against real token counts, so
    it must count exactly what the API sends (and nothing else)."""
    total = 0
    for m in msgs or []:
        total += len(str(m.get("content") or ""))
        for tc in (m.get("tool_calls") or []):
            try:
                import json
                total += len(json.dumps(tc))
            except Exception:
                total += 64
        total += 24   # role/framing overhead
    if tools:
        try:
            import json
            total += len(json.dumps(tools))
        except Exception:
            total += len(tools) * 225
    return total


def window(lane: str, cfg_ctx: Optional[int] = None, n_slots: int = 1,
           kv_unified: bool = False) -> int:
    """Prompt window for a lane without touching the server.

    With a unified KV pool (`-kvu`) every slot shares the whole `-c` pool, so the
    per-request window is `cfg_ctx`; otherwise llama-server divides `-c` across
    slots. Mirrors routes/common.py::main_ctx_tokens and core/small_model.py's
    launch flags.

    The resolved value is remembered so `snapshot()` can report it, but
    `probed_at` is deliberately left at 0: that marks it as config-derived, so a
    later `/slots` probe still overrides it."""
    st = _state(lane)
    if st["window"]:
        return int(st["window"])
    ctx = int(cfg_ctx or 0)
    if ctx <= 0:
        return 0
    slots = max(1, int(n_slots or 1))
    resolved = max(MIN_WINDOW, ctx) if (kv_unified or slots == 1) else max(MIN_WINDOW, ctx // slots)
    st["window"] = resolved
    return resolved


def set_window(lane: str, tokens: int) -> None:
    """Record a window obtained elsewhere (e.g. a /slots probe)."""
    if tokens and int(tokens) > 0:
        st = _state(lane)
        st["window"] = int(tokens)
        st["probed_at"] = time.time()


async def probe_window(lane: str, client) -> Optional[int]:
    """Ask the running llama-server for the real per-request window.

    `/slots` advertises `n_ctx` per slot, which is the number it quotes when it
    rejects an oversized prompt - authoritative, unlike profile math. Cached for
    PROBE_TTL_S and backed off for PROBE_RETRY_S after a failure, so a step never
    pays for this twice. Never raises: a probe failure just keeps the fallback."""
    if client is None:
        return None
    st = _state(lane)
    now = time.time()
    if st["window"] and now - st["probed_at"] < PROBE_TTL_S:
        return int(st["window"])
    if st["probe_failed_at"] and now - st["probe_failed_at"] < PROBE_RETRY_S:
        return int(st["window"]) if st["window"] else None
    try:
        r = await client.get("/slots", timeout=1.5)
        if getattr(r, "status_code", 0) != 200:
            raise RuntimeError(f"HTTP {getattr(r, 'status_code', '?')}")
        slots = r.json()
        if isinstance(slots, dict):          # some builds wrap the list
            slots = slots.get("slots") or []
        if not isinstance(slots, list) or not slots:
            raise RuntimeError("no slots reported")
        n_ctx = (slots[0] or {}).get("n_ctx")
        if not n_ctx or int(n_ctx) <= 0:
            raise RuntimeError("slot reports no n_ctx")
        set_window(lane, int(n_ctx))
        return int(n_ctx)
    except Exception:
        st["probe_failed_at"] = now
        return int(st["window"]) if st["window"] else None


def factor(lane: str) -> float:
    """Observed actual/estimated prompt-token ratio for this lane (EMA, >= 1.0)."""
    return float(_state(lane)["factor"])


def cloud_cap() -> int:
    """context.cloud_budget_tokens: the most a cloud lane's prompt may grow to. Cloud windows are huge (262k), so
    0.7 x window (~183k) let a long run re-send 100k+ tokens on every step; 0 = no cap."""
    try:
        from .small_model import APP_CONFIG
        return max(0, int((APP_CONFIG.get("context") or {}).get("cloud_budget_tokens", 60000)))
    except Exception:
        return 60000


def margin() -> float:
    """Fraction of the window a prompt may fill before compaction (config: context.compaction_threshold)."""
    try:
        from .small_model import APP_CONFIG
        v = float((APP_CONFIG.get("context") or {}).get("compaction_threshold", MARGIN))
        return min(max(v, 0.3), 0.95)
    except Exception:
        return MARGIN


def budget_for(lane: str, window_tokens: int, cloud: bool = False) -> int:
    """Token budget for one request: MARGIN of the window, tightened by the
    estimator's observed error so compaction fires before the server refuses.
    `cloud`: the lane runs on a provider, where the window is not the limit that matters (cost is): cap it."""
    win = int(window_tokens or 0)
    if win <= 0:
        return 0
    b = max(1, int(win * margin() / max(SAFETY_MIN, factor(lane))))
    cap = cloud_cap() if cloud else 0
    return min(b, cap) if cap else b


def prompt_tokens_for(lane: str, msgs: list, tools: Optional[list] = None) -> int:
    """Tokens the next request will cost, anchored on the server's last exact count.

    Anchored path (a previous request in this lane reported its prompt tokens and
    the prompt has only grown since): exact base + one delta, so the error is
    bounded by the newest message instead of compounding over the history.
    Otherwise: the plain estimate scaled by the learned safety factor, which is
    always >= the raw estimate since the factor is clamped to [1.0, 2.0].
    Both paths take the plain estimate into account so a bad anchor can never
    report *less* than the estimator believes."""
    from .agent_loop import estimate_prompt_tokens   # deferred: avoids an import cycle
    st = _state(lane)
    chars = prompt_chars(msgs, tools)
    plain = int(estimate_prompt_tokens(msgs, tools) * st["factor"])
    last_actual, last_chars = st["last_actual"], st["last_chars"]
    if last_actual and last_chars is not None and chars >= last_chars:
        try:
            import math
            delta = chars - last_chars
            anchored = int(last_actual) + int(math.ceil(delta / max(1.0, st["ratio"])))
            return max(anchored, plain)
        except Exception:
            return plain
    return plain


def record_usage(lane: str, estimated: int, actual: int,
                 msgs: Optional[list] = None, tools: Optional[list] = None) -> None:
    """Feed one real `usage.prompt_tokens` back into the accounting.

    `estimated` must be the estimate made for *that* request (post-compaction),
    and `msgs`/`tools` the exact request payload, so the learned ratio measures
    what actually varies. Callers must skip this when `actual` came from a
    fallback rather than the server."""
    try:
        actual = int(actual or 0)
        estimated = int(estimated or 0)
    except (TypeError, ValueError):
        return
    if actual <= 0:
        return
    st = _state(lane)
    chars = prompt_chars(msgs, tools) if msgs is not None else None
    if chars is not None and st["last_chars"] is not None and st["last_actual"]:
        d_chars, d_toks = chars - st["last_chars"], actual - int(st["last_actual"])
        if d_chars >= MIN_DELTA_CHARS and d_toks > 0:
            ratio = _clamp(d_chars / d_toks, CHARS_PER_TOKEN_MIN, CHARS_PER_TOKEN_MAX)
            st["ratio"] = (1 - EMA_ALPHA) * st["ratio"] + EMA_ALPHA * ratio
    if estimated > 0:
        sample = _clamp(actual / estimated, SAFETY_MIN, SAFETY_MAX)
        st["factor"] = _clamp((1 - EMA_ALPHA) * st["factor"] + EMA_ALPHA * sample,
                              SAFETY_MIN, SAFETY_MAX)
    st["last_actual"], st["last_chars"] = actual, chars


def snapshot() -> dict:
    """Read-only view for /control/status and the UI chip: what the loop believes
    about each lane, so a wrong window or an inflated factor is visible."""
    out = {}
    for lane, st in _STATE.items():
        out[lane] = {
            "window": st["window"], "window_is_probed": bool(st["probed_at"]),
            "factor": round(float(st["factor"]), 3),
            "chars_per_token": round(float(st["ratio"]), 3),
            "last_prompt_tokens": st["last_actual"], "budget": budget_for(lane, st["window"] or 0),
        }
        # Include TPS if recorded
        tps = _TPS_STATE.get(lane)
        if tps and tps.get("ema_tps") is not None:
            out[lane]["ema_tps"] = round(tps["ema_tps"], 1)
            out[lane]["last_tps"] = round(tps.get("last_tps", 0.0), 1)
    return out


# ---------------------------------------------------------------------------
# Phase 2: Real tok/s EMA tracking & latency advisor
# ---------------------------------------------------------------------------
# lane -> {ema_tps, last_tps, n_samples, last_recorded_at}
_TPS_STATE: dict = {}

# Latency targets
_TTFT_TARGET_MS = 800    # first token budget
_TBT_TARGET_TPS = 20.0  # minimum acceptable tok/s for smooth UI


def _tps_state(lane: str) -> dict:
    st = _TPS_STATE.get(lane)
    if st is None:
        st = _TPS_STATE[lane] = {
            "ema_tps": None, "last_tps": 0.0,
            "n_samples": 0, "last_recorded_at": 0.0,
        }
    return st


def record_tps(lane: str, tokens_generated: int, elapsed_s: float) -> None:
    """Record one generation's real tok/s into the per-lane EMA.

    Args:
        lane: lane name (e.g. "main", "executor").
        tokens_generated: completion_tokens from usage or the actual count from
            the SSE stream (not prompt_tokens — those are prefill throughput).
        elapsed_s: wall-clock seconds from first to last token in the response.
            Callers can measure this from SSE timing or from
            ``usage.completion_tokens / (tg_ts reported by server)`` when the
            server embeds timing in the final SSE chunk.
    """
    if elapsed_s <= 0 or tokens_generated <= 0:
        return
    tps = tokens_generated / elapsed_s
    st = _tps_state(lane)
    if st["ema_tps"] is None:
        # cold start: seed with the first observation
        st["ema_tps"] = tps
    else:
        # Use a faster alpha for the first 10 samples (converge faster from cold)
        alpha = 0.40 if st["n_samples"] < 10 else 0.20
        st["ema_tps"] = (1 - alpha) * st["ema_tps"] + alpha * tps
    st["last_tps"] = tps
    st["n_samples"] += 1
    st["last_recorded_at"] = time.time()


def tps_snapshot() -> dict:
    """Per-lane tok/s EMA snapshot. Safe to call from any thread."""
    return {
        lane: {
            "ema_tps": round(float(st["ema_tps"]), 1) if st["ema_tps"] is not None else None,
            "last_tps": round(float(st["last_tps"]), 1),
            "n_samples": st["n_samples"],
        }
        for lane, st in _TPS_STATE.items()
    }


def reset_tps(lane: Optional[str] = None) -> None:
    """Forget TPS observations (on model reload or manual reset)."""
    if lane is None:
        _TPS_STATE.clear()
    else:
        _TPS_STATE.pop(lane, None)


def budget_latency_advice(lane: str, profile: Optional[dict] = None,
                          system_prompt: str = "",
                          tools_json: str = "",
                          prompt_tokens: int = 2048,
                          task_type: str = "coding") -> dict:
    """Return latency tuning advice for a lane based on observed tok/s.

    Integrates the EMA tok/s observed on this lane with the streaming_profile
    advisor.  Never mutates the profile; advisory-only.

    Returns:
        {
          "ema_tps": float | None,  # observed generation speed
          "target_tps": float,      # minimum acceptable
          "below_target": bool,     # True when tuning is needed
          "advice": dict,           # from streaming_profile.full_latency_profile()
        }
    """
    tps_st = _tps_state(lane)
    ema_tps = tps_st.get("ema_tps")
    below_target = (ema_tps is not None) and (ema_tps < _TBT_TARGET_TPS)

    try:
        from .streaming_profile import full_latency_profile
        advice = full_latency_profile(
            profile=profile or {},
            system_prompt=system_prompt,
            tools_json=tools_json,
            prompt_tokens=prompt_tokens,
            task_type=task_type,
        )
    except Exception:
        advice = {}

    return {
        "lane": lane,
        "ema_tps": round(ema_tps, 1) if ema_tps is not None else None,
        "target_tps": _TBT_TARGET_TPS,
        "below_target": below_target,
        "advice": advice,
    }


