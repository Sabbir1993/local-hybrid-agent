"""A plan drafted for a model that would not write one.

The plan gate (routes/agent/stream.py) offers only create_plan until a plan exists. A small model sometimes keeps
calling other tools anyway; after two refusals the gate used to give up and let the run continue with no plan at all.
Instead one short, local, JSON-only call drafts the steps and the loop stores them exactly like a create_plan call.
Local only (force_local): the request text never goes to a cloud lane because of this helper.
"""
import asyncio
import json
import re
import sys
from typing import Optional

DRAFT_TIMEOUT_S = 45
MAX_STEPS = 8

_SYSTEM = (
    "You split a user's request into an ordered task graph (DAG). Reply with JSON only, no other text: "
    '{"tasks": [{"id": 1, "title": "step description", "phase": "recon|act|verify", "deps": []}]}. '
    "3 to 8 steps, each one short sentence naming one checkable outcome (one file, one command, one finding). "
    "Use 'deps' for step IDs that must finish first (e.g. [1]). Do not do the work and do not add a step that only says to finish."
)


def parse_steps(text: str) -> list:
    """The steps/tasks from a model reply, or []. Tolerates <think> blocks, code fences and prose around the JSON.
    Returns list of step strings or task dicts with backward compatibility."""
    text = re.sub(r"<think>[\s\S]*?</think>", "", text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []
    raw = data.get("tasks") or data.get("steps")
    if not isinstance(raw, list):
        return []
    steps = []
    for i, s in enumerate(raw, start=1):
        if isinstance(s, dict):
            title = str(s.get("title") or s.get("text") or "").strip()
            if not title or title.lower() == "none":
                continue
            deps = s.get("deps")
            if not isinstance(deps, list):
                deps = [i - 1] if i > 1 else []
            else:
                deps = [int(d) for d in deps if isinstance(d, (int, str)) and str(d).isdigit()]
            steps.append({
                "ord": int(s.get("id") or s.get("ord") or i),
                "text": title,
                "phase": s.get("phase"),
                "deps": deps,
                "verification": s.get("verification")
            })
        else:
            s_str = str(s).strip()
            if s_str and s_str.lower() != "none":
                steps.append(s_str)
    return steps[:MAX_STEPS] if len(steps) >= 2 else []


async def draft_steps(request: str, user_id: Optional[int] = None) -> list:
    """Draft 2-8 plan steps for `request` on a local lane; [] when no model answers usefully."""
    request = (request or "").strip()
    if not request:
        return []
    from core import lanes
    payload = {
        "messages": [{"role": "system", "content": _SYSTEM},
                     {"role": "user", "content": request[:4000]}],
        "temperature": 0.1,
        "max_tokens": 400,
        "stream": False,
    }
    try:
        data, _t = await asyncio.wait_for(
            lanes.post_chat("agent.tool_step", payload, user_id, force_local=True), timeout=DRAFT_TIMEOUT_S)
        return parse_steps(lanes.message_text(data))
    except Exception as e:
        print(f"[auto_plan] draft unavailable: {type(e).__name__}: {e}", file=sys.stderr)
        return []
