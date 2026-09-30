"""The messages a small executor sees: this request and this run's steps, not the whole chat.

The main lane keeps the full conversation. A 4B executor handed a long chat - old
answers, old tool results, earlier tasks - loses the thread and answers about the
wrong thing, and pays for thousands of prompt tokens on every step. So the executor
gets:

  * the system messages,
  * the last few prior text turns, each cut short (enough to resolve "fix it" or
    "continue", without dragging old tool output along),
  * the current request and everything this run has done since, with oversized tool
    results cut.

The view is rebuilt from the full history on each step, never stored, so the main
lane and any escalation still see everything.
"""

import json
from typing import Optional

from ..text_clip import clip_head_tail

PRIOR_TURNS = 2            # prior user/assistant text messages kept
PRIOR_CHARS = 400          # each, truncated
TOOL_RESULT_CHARS = 6000   # tool results inside the current run (head + tail, with a note saying the tool finished normally)

# Messages the loop injects into the conversation itself. They are role "user" but are not
# something the person typed, so they never mark the start of the current request.
_CONTROL_PREFIXES = ("[continue]", "[plan reminder]", "[plan incomplete]", "[plan audit required]",
                     "[stopped]", "[stuck]", "[system]", "[budget]")


def _text(content) -> str:
    """Plain text of a message body (string or multimodal parts list)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                if p.get("type") == "text":
                    parts.append(str(p.get("text") or ""))
                elif p.get("type") in ("image_url", "input_image"):
                    parts.append("[image]")
        return " ".join(x for x in parts if x)
    return "" if content is None else str(content)


def _is_control(msg: dict) -> bool:
    if msg.get("role") != "user":
        return False
    t = _text(msg.get("content")).lstrip()
    return t.lower().startswith(_CONTROL_PREFIXES)


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n].rstrip() + " …"


def current_request_index(msgs: list) -> Optional[int]:
    """Index of the message the person last typed, skipping loop-injected control turns."""
    for i in range(len(msgs) - 1, -1, -1):
        m = msgs[i]
        if m.get("role") == "user" and not _is_control(m):
            return i
    return None


def build_executor_view(msgs: list, prior_turns: int = PRIOR_TURNS, prior_chars: int = PRIOR_CHARS,
                        tool_chars: int = TOOL_RESULT_CHARS) -> list:
    """Executor's messages: system + a few clipped prior turns + the current run in full."""
    idx = current_request_index(msgs)
    if idx is None:
        return msgs
    head = [m for m in msgs[:idx] if m.get("role") == "system"]
    prior = []
    for m in msgs[:idx]:
        if m.get("role") not in ("user", "assistant") or m.get("tool_calls"):
            continue
        t = _text(m.get("content")).strip()
        if t:
            prior.append({"role": m["role"], "content": _clip(t, prior_chars)})
    prior = prior[-max(0, prior_turns):] if prior_turns else []
    # a kept history must open on a user message so roles keep alternating
    while prior and prior[0]["role"] != "user":
        prior.pop(0)

    current = []
    for m in msgs[idx:]:
        if m.get("role") == "tool":
            body = _text(m.get("content"))
            clipped = clip_head_tail(body, tool_chars)
            if clipped is not body:
                m = dict(m)
                m["content"] = clipped
        current.append(m)
    return head + prior + current


def dropped(msgs: list, view: list) -> int:
    """How many messages the view leaves out (for telemetry / the ctx event)."""
    return max(0, len(msgs) - len(view))
