"""Tool-result clearing: the light first step of context management.

Old tool results are the bulk of a long run's prompt (file reads, command output, page text) and the model
rarely needs them again: what matters is what it learned, which is already in its own later messages. Once the
prompt passes `context.clear_trigger_tokens`, the bodies of all but the newest `clear_keep_results` tool results
are replaced by a one-line placeholder that says what the call was and how big its result was, so the model can
call the tool again if it really needs it. The assistant's tool_call records stay, so the model still sees what
it did. Results remain in the UI and the database; only the copy sent to the model shrinks.

Two rules keep the provider's prompt cache useful:
  * it fires in batches: only when at least `clear_at_least_tokens` would be freed, then runs dry until the
    prompt grows past the trigger again (clearing rewrites the prompt from the first cleared result on);
  * nothing is ever rewritten otherwise (same rule as core/agent_loop/tool_output.py).

This is what the Anthropic API calls `clear_tool_uses` (trigger ~30-100k, keep 3-6, `[cleared to save context]`);
compaction (core/agent_loop/compaction.py) stays as the next layer for when clearing is not enough.
"""

from typing import Optional

from .compaction import _call_index, _call_label

CLEARED_PREFIX = "[cleared to save context"
READ_DIGEST_CHARS = 700            # ...and never more than this many characters of them
READ_DIGEST_LINES = 15             # lines of a cleared file read that stay as a digest
MIN_CLEAR_CHARS = 600              # a result this small is not worth a placeholder
# never cleared: what the model must keep seeing, or that is already tiny
EXEMPT_TOOLS = frozenset({"read_skill", "create_plan", "update_plan_item", "get_plan", "finish"})


def _cfg() -> dict:
    try:
        from ..small_model import APP_CONFIG
        return (APP_CONFIG.get("context") or {})
    except Exception:
        return {}


def settings(squeeze: bool = False) -> dict:
    """{trigger, keep, at_least} in tokens / results. `squeeze` (a run close to its token budget) clears harder."""
    c = _cfg()

    def num(key, default, lo):
        try:
            return max(lo, int(c.get(key, default)))
        except (TypeError, ValueError):
            return default
    trig = num("clear_trigger_tokens", 50000, 0)
    keep = num("clear_keep_results", 10, 1)
    least = num("clear_at_least_tokens", 8000, 0)
    if squeeze:
        trig, keep, least = max(1, trig // 3), max(1, keep // 2), max(1, least // 2)
    return {"trigger": trig, "keep": keep, "at_least": least}


def _is_cleared(m: dict) -> bool:
    return str(m.get("content") or "").startswith(CLEARED_PREFIX)


def clear_old_results(msgs: list, prompt_tokens: int, squeeze: bool = False) -> int:
    """Replace old tool-result bodies in `msgs` (in place) when the prompt is past the trigger.
    Returns the estimated tokens freed (0 when nothing was cleared). `prompt_tokens` is the caller's estimate of the
    whole next request (system + tools + history)."""
    s = settings(squeeze)
    if s["trigger"] <= 0 or prompt_tokens <= s["trigger"]:
        return 0
    tool_idx = [i for i, m in enumerate(msgs) if m.get("role") == "tool"]
    if len(tool_idx) <= s["keep"]:
        return 0
    calls = _call_index(msgs)
    eligible = []
    for i in tool_idx[:-s["keep"]]:
        m = msgs[i]
        body = str(m.get("content") or "")
        name = (calls.get(m.get("tool_call_id")) or ("", {}))[0]
        if name in EXEMPT_TOOLS or _is_cleared(m) or len(body) < MIN_CLEAR_CHARS:
            continue
        eligible.append(i)
    freed_chars = sum(len(str(msgs[i].get("content") or "")) for i in eligible)
    if not eligible or freed_chars // 4 < s["at_least"]:
        return 0          # not worth rewriting the cached prefix yet
    for i in eligible:
        m = msgs[i]
        body = str(m.get("content") or "")
        call = calls.get(m.get("tool_call_id"))
        label = _call_label(*call) if call else "tool result"
        if call and call[0] == "read_file":
            # a bare "call it again" made the model read the same file over and over: keep what it saw
            # (the top of the file) so it can tell whether it really needs the rest
            head = "\n".join(body.splitlines()[:READ_DIGEST_LINES])[:READ_DIGEST_CHARS]
            msgs[i] = {**m, "content": f"{CLEARED_PREFIX}: {label}, about {len(body) // 4} tokens omitted. "
                                       "You already read this: rely on what you noted, and re-read only the exact "
                                       "lines you still need (offset/limit). Top of it:\n" + head + "\n...]"}
            continue
        msgs[i] = {**m, "content": f"{CLEARED_PREFIX}: {label}, about {len(body) // 4} tokens omitted. "
                                   "Call the tool again if you need it.]"}
    return freed_chars // 4
