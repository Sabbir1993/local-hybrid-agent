"""Capping a tool result before it enters the conversation history.

Every later step re-sends the whole history, so one 200 KB `read_file` or a noisy build log is paid
for on every following call. The UI still gets the full result; only the copy the model keeps is
capped, keeping the head (where errors and headers are) and the tail (where the summary line is).
It is done once, when the result is added, and never revised: rewriting an old message would
change the prompt prefix and throw away the provider's cache, which costs more than it saves.

The cap is `agent.tool_result_chars` in config/app.json (default 12000, 0 = off).
"""

from typing import Optional

DEFAULT_CAP = 12000
MIN_CAP = 1000          # below this a cap would cut ordinary results
_SLACK = 300            # room for the omission note, so a capped result is never capped again


def cap_from_config(agent_cfg: Optional[dict]) -> int:
    try:
        v = int((agent_cfg or {}).get("tool_result_chars", DEFAULT_CAP))
    except (TypeError, ValueError):
        return DEFAULT_CAP
    return 0 if v <= 0 else max(MIN_CAP, v)


def cap_tool_result(result, cap: int = DEFAULT_CAP):
    """`result` unchanged when short (or not text); else head + tail with a note on what was cut."""
    if not cap or not isinstance(result, str) or len(result) <= cap + _SLACK:
        return result
    head = int(cap * 0.7)
    tail = cap - head
    omitted = len(result) - head - tail
    return (result[:head].rstrip()
            + f"\n... [{omitted} characters omitted to keep the conversation small; "
              "read a narrower range with read_file_chunk, or filter the command's output] ...\n"
            + result[-tail:].lstrip())
