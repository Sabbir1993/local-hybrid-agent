"""Turning a finish(answer) tool call into the run's final reply."""

import json
from typing import Optional

FINISH_TOOL = "finish"
FINISH_TOOLS = frozenset({"finish", "complete_task"})


def _args(call: dict) -> dict:
    raw = (call.get("function") or {}).get("arguments")
    if isinstance(raw, dict):
        return raw
    try:
        val = json.loads(raw or "{}")
        return val if isinstance(val, dict) else {}
    except (TypeError, ValueError):
        return {"answer": str(raw or "")}


def _format_finish_reply(call: dict) -> str:
    name = (call.get("function") or {}).get("name")
    args = _args(call)
    if name == "complete_task":
        summary = str(args.get("summary") or args.get("answer") or "").strip()
        files = args.get("files_modified") or []
        verif = str(args.get("verification") or "").strip()
        parts = []
        if summary:
            parts.append(summary)
        if isinstance(files, list) and files:
            parts.append("\n**Modified Files:**\n" + "\n".join(f"- `{f}`" for f in files))
        if verif:
            parts.append(f"\n**Verification:**\n{verif}")
        return "\n\n".join(parts).strip() if parts else summary
    return str(args.get("answer") or "").strip()


def is_finish(call: dict) -> bool:
    return (call.get("function") or {}).get("name") in FINISH_TOOLS


def apply_finish(content: str, tool_calls) -> tuple:
    """(content, tool_calls, finished).

    finish/complete_task alone -> its answer becomes the reply and there are no tool calls, so the
                                  normal final-reply path (checks, validation, verifier) handles it.
    finish + other calls       -> the finish is dropped: do that work first, finish afterwards.
    """
    calls = list(tool_calls or [])
    fin = [c for c in calls if is_finish(c)]
    if not fin:
        return content, calls, False
    others = [c for c in calls if not is_finish(c)]
    if others:
        return content, others, False
    answer = _format_finish_reply(fin[0])
    return answer, [], True


def without_finish(tools: Optional[list]) -> Optional[list]:
    """The tool list minus finish and complete_task (a step that must be a real tool call)."""
    if not tools:
        return tools
    return [t for t in tools if (t.get("function") or {}).get("name") not in FINISH_TOOLS]

