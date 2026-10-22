"""Model-facing tools: what is running in the background, how far along, and stop."""

from ..registry import registry
from ..request_context import get_current_user_id
from . import service


def _uid():
    return get_current_user_id()


def _line(t: dict) -> str:
    bits = [t["state"]] + ([t["stage"]] if t.get("stage") else []) + ([f"step {t['step']}"] if t.get("step") else [])
    return f"{t['id']}  [{t['kind']}]  {t['title']}  - {', '.join(bits)}" + (f" ({t['reason']})" if t.get("reason") else "")


async def tool_task_list(args: dict) -> str:
    uid = _uid()
    if uid is None:
        return "error: no signed-in user"
    tasks = service.list_tasks(uid, args.get("kind"), args.get("state"), int(args.get("limit") or 20))
    if not tasks:
        return "no background tasks"
    return f"{len(tasks)} task(s):\n" + "\n".join(_line(t) for t in tasks)


async def tool_task_check(args: dict) -> str:
    uid, tid = _uid(), str(args.get("id") or "").strip()
    t = service.get_task(uid, tid) if uid is not None and tid else None
    if t is None:
        return "error: unknown task id"
    out = [_line(t), f"running for {t['age_s']}s" if t["state"] == "running" else f"ended: {t['state']}"]
    out.append("can stop: " + ("yes" if t["can_stop"] else "no"))
    return "\n".join(out)


async def tool_task_stop(args: dict) -> str:
    uid, tid = _uid(), str(args.get("id") or "").strip()
    r = service.stop_task(uid, tid) if uid is not None and tid else None
    if r is None:
        return "error: unknown task id"
    return f"{'stopped' if r['stopped'] else 'not stopped'}: {r['note']}"


def _fn(name, desc, props, required=()):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": list(required)}}}


TASK_TOOLS = [
    ("task_list", tool_task_list, _fn(
        "task_list", "List the user's background tasks (agent runs, sub-agents, media jobs, knowledge ingests): "
        "running first, then recent. Use when asked what is running or how a background job is going.",
        {"kind": {"type": "string", "enum": list(service.TITLES)},
         "state": {"type": "string", "description": "'active' for what is running now, or running/completed/failed/cancelled/interrupted"},
         "limit": {"type": "integer"}})),
    ("task_check", tool_task_check, _fn(
        "task_check", "Status of one background task by id (from task_list).", {"id": {"type": "string"}}, ["id"])),
    ("task_stop", tool_task_stop, _fn(
        "task_stop", "Stop a running background task by id. Only when the user asks for it.",
        {"id": {"type": "string"}}, ["id"])),
]


def register_task_tools() -> None:
    for name, fn, schema in TASK_TOOLS:
        registry.register(name, fn, schema, source="tasks", meta={"label": name.replace("_", " ")}, replace=True)
