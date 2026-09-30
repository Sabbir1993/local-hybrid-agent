"""Capping a tool result before it enters the conversation history.

Every later step re-sends the whole history, so one 200 KB `read_file` or a noisy build log is paid
for on every following call. The UI still gets the full result; only the copy the model keeps is
capped, keeping the head (where errors and headers are) and the tail (where the summary line is).
It is done once, when the result is added, and never revised: rewriting an old message would
change the prompt prefix and throw away the provider's cache, which costs more than it saves.

The cap is `agent.tool_result_chars` in config/app.json (default 6000, 0 = off).
"""

from typing import Optional

from ..text_clip import clip_head_tail

DEFAULT_CAP = 6000
MIN_CAP = 1000          # below this a cap would cut ordinary results


def cap_from_config(agent_cfg: Optional[dict]) -> int:
    try:
        v = int((agent_cfg or {}).get("tool_result_chars", DEFAULT_CAP))
    except (TypeError, ValueError):
        return DEFAULT_CAP
    return 0 if v <= 0 else max(MIN_CAP, v)


def cap_tool_result(result, cap: int = DEFAULT_CAP):
    """`result` unchanged when short (or not text); else head + tail with a note on what was cut."""
    return clip_head_tail(result, cap)
