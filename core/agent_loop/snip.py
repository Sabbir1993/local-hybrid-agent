"""Narration snipping: the context-management layer between clearing (tool results)
and compaction (digest).

clearing.py (core/agent_loop/clearing.py) stubs old *tool result* bodies; compaction.py
rolls whole turns into a digest only when the prompt passes the compaction threshold
(context.compaction_threshold, 0.70 of the window). What neither touches is old
*assistant narration*: the prose an assistant message carries beside (or without)
its tool calls. In long runs that narration is a growing share of the prompt even
after every tool result has been cleared, and it is paid for on every request,
exactly like an uncapped tool result.

This layer stubs it with the same discipline as clearing:

  * fires on a token trigger (context.snip.trigger_tokens), and in batches --
    only when at least context.snip.at_least_tokens would be freed, because a
    snip rewrites the prompt from the first snipped message on and would throw
    away the provider's/llama.cpp's reused KV prefix otherwise;
  * only role == "assistant" prose is stubbed, never the system prompt, never a
    user turn, never a tool result (those belong to compaction and clearing);
  * tool_calls are preserved untouched (only the "content" field is rewritten),
    so the action record and the assistant/tool pairing survive -- the model
    still sees what it did, just not the prose about doing it;
  * the newest context.snip.keep_recent_narration assistant messages stay
    verbatim (and in `squeeze` mode, half of them: the current step's own
    narration always stays);
  * idempotent: a stub is marked with SNIP_PREFIX and never snipped again.

The stub says what was omitted and why nothing above it was lost -- the shape of
the message the model reads stays the same as clearing's "[cleared to save
context" family, so no tool re-read loop is invited.

DEFAULT-OFF. Killed by `context.snip.enabled` (absent/false => strict no-op, the
guarantee tests/test_context_snip.py::NoOpGuarantee pins). It is armed only after
an offline A/B (tests/eval_mock.py long-run slice) shows freed context with no
regression in answered/synth rates -- the same rule E-dispatch died by.

Config: context.snip.{enabled, trigger_tokens, keep_recent_narration, min_chars,
at_least_tokens} in config/app.json.
"""

SNIP_PREFIX = "[snipped to save context"
MIN_SNIPELLABLE_CHARS = 600     # a narration shorter than this is not worth a placeholder

# never snipped, whatever the flag says the knobs are: the loop nags here
EXEMPT_MARKERS = ("[snipped to save context", "[cleared to save context")


def _cfg() -> dict:
    try:
        from ..small_model import APP_CONFIG
        return ((APP_CONFIG.get("context") or {}).get("snip") or {})
    except Exception:
        return {}


def _enabled() -> bool:
    """strict no-op unless explicitly turned on; a broken config is off."""
    return bool(_cfg().get("enabled", False))


def settings(squeeze: bool = False) -> dict:
    """{trigger, keep, least, min_chars}. `squeeze` (the run is close to its
    token budget) snips harder, like clearing does."""
    c = _cfg()

    def num(key, default, lo):
        try:
            return max(lo, int(c.get(key, default)))
        except (TypeError, ValueError):
            return default
    trig = num("trigger_tokens", 50000, 0)
    keep = num("keep_recent_narration", 3, 1)
    least = num("at_least_tokens", 8000, 0)
    min_chars = num("min_chars", MIN_SNIPELLABLE_CHARS, 1)
    if squeeze:
        trig, keep, least = max(1, trig // 3), max(1, keep // 2), max(1, least // 2)
    return {"trigger": trig, "keep": keep, "least": least, "min_chars": min_chars}


def _snippable(m: dict, min_chars: int) -> bool:
    """Old assistant prose worth a placeholder. tool_calls are NOT a blocker:
    only "content" is rewritten, the action record beside it stays."""
    if not isinstance(m, dict):
        return False
    if m.get("role") != "assistant":
        return False                            # system/user/tool results are other layers' business
    body = str(m.get("content") or "")
    if len(body) < min_chars:
        return False
    return not any(body.startswith(p) for p in EXEMPT_MARKERS)


def snip_old_narration(msgs: list, prompt_tokens: int, squeeze: bool = False) -> int:
    """Stub old assistant narration in `msgs` (in place) when the prompt is past
    the trigger. Returns the estimated tokens freed (0 when nothing was snipped).
    `prompt_tokens` is the caller's estimate of the whole next request (system +
    tools + history), the same value clearing's caller already computed."""
    s = settings(squeeze)
    if not _enabled() or s["trigger"] <= 0 or prompt_tokens <= s["trigger"]:
        return 0
    # assistant messages in history order; the newest `keep` are untouchable.
    # Indexes: snip works oldest-first so the stub boundary only ever moves
    # toward the tail as the run grows (nothing recent is ever rewritten).
    idxs = [i for i, m in enumerate(msgs) if isinstance(m, dict) and m.get("role") == "assistant"]
    if len(idxs) <= s["keep"]:
        return 0
    eligible = [i for i in idxs[:-s["keep"]] if _snippable(msgs[i], s["min_chars"])]
    if not eligible:
        return 0
    freed_chars = sum(len(str(msgs[i].get("content") or "")) for i in eligible)
    if freed_chars // 4 < s["least"]:
        return 0                                # not worth rewriting the reused prefix yet
    for i in eligible:
        body = str(msgs[i].get("content") or "")
        has_calls = bool(msgs[i].get("tool_calls"))
        msgs[i] = {**msgs[i], "content": (
            f"{SNIP_PREFIX}: earlier assistant narration, about {len(body) // 4} tokens omitted. "
            "The actions and their results below and the recent steps are unchanged"
            + ("; this step's tool calls are listed beside the actions" if has_calls else "") + ".]")}
    return freed_chars // 4
