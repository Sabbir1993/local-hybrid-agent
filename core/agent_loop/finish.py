"""Turning a finish(answer) tool call into the run's final reply."""

import json
from typing import Optional

FINISH_TOOL = "finish"


def _args(call: dict) -> dict:
    raw = (call.get("function") or {}).get("arguments")
    if isinstance(raw, dict):
        return raw
    try:
        val = json.loads(raw or "{}")
        return val if isinstance(val, dict) else {}
    except (TypeError, ValueError):
        return {"answer": str(raw or "")}


def is_finish(call: dict) -> bool:
    return (call.get("function") or {}).get("name") == FINISH_TOOL


def apply_finish(content: str, tool_calls) -> tuple:
    """(content, tool_calls, finished).

    finish alone       -> its answer becomes the reply and there are no tool calls, so the
                          normal final-reply path (checks, validation, verifier) handles it.
    finish + other calls -> the finish is dropped: do that work first, finish afterwards.
    """
    calls = list(tool_calls or [])
    fin = [c for c in calls if is_finish(c)]
    if not fin:
        return content, calls, False
    others = [c for c in calls if not is_finish(c)]
    if others:
        return content, others, False
    answer = str(_args(fin[0]).get("answer") or "").strip()
    return answer, [], True


def without_finish(tools: Optional[list]) -> Optional[list]:
    """The tool list minus finish (a step that must be a real tool call)."""
    if not tools:
        return tools
    return [t for t in tools if (t.get("function") or {}).get("name") != FINISH_TOOL]
