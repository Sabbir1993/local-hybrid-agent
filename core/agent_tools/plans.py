import contextvars
from typing import Optional

_plan_session_var: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "plan_session_id", default=None)

_PLAN_STATUS_MARKS = {"pending": "☐", "in_progress": "⏳", "done": "✅", "failed": "❌"}


def set_plan_context(session_id) -> None:
    """Point the plan tools at the current agent session (called from routes/agent.py)."""
    try:
        _plan_session_var.set(int(session_id) if session_id is not None else None)
    except (TypeError, ValueError):
        _plan_session_var.set(None)


def get_plan_context() -> Optional[int]:
    return _plan_session_var.get()


def _format_plan(items: list) -> str:
    if not items:
        return "(no plan tracked for this session yet — call create_plan)"
    lines = []
    for it in items:
        mark = _PLAN_STATUS_MARKS.get(it.get("status", "pending"), "☐")
        line = f"{it['ord']}. {mark} {it['text']}"
        if it.get("note"):
            line += f"  — {it['note']}"
        lines.append(line)
    return "\n".join(lines)


def tool_create_plan(args: dict) -> str:
    from ..db import db_get_plan_items, db_set_plan_item_status, db_set_plan_items
    if get_plan_context() is None:
        raise ValueError("no active session for plan tracking (session_id missing from /agent/run)")
    raw = args.get("items")
    if isinstance(raw, str):
        raw = [s.strip() for s in raw.replace(";", "\n").split("\n") if s.strip()]
    if not isinstance(raw, list) or not raw:
        raise ValueError("items must be a non-empty array of step description strings")
    from .limits import agent_limit
    max_items, max_chars = agent_limit("plan_max_items"), agent_limit("plan_item_max_chars")
    texts = []
    for it in raw:
        t = str(it.get("text") or "").strip() if isinstance(it, dict) else str(it).strip()
        if t:
            texts.append(t)
    if not texts:
        raise ValueError("plan contained no usable step text")
    if len(texts) > max_items:
        raise ValueError(f"the plan has {len(texts)} steps; the limit is {max_items}. Group related work into "
                         "fewer, bigger steps (each step is one file or one checkable outcome) and call "
                         "create_plan again.")
    long = [i + 1 for i, t in enumerate(texts) if len(t) > max_chars]
    if long:
        raise ValueError(f"step(s) {', '.join(map(str, long[:5]))} are longer than {max_chars} characters. "
                         "Keep each step to one short sentence and call create_plan again.")
    from ..agent_loop import plan_guard
    sid = get_plan_context()
    existing = db_get_plan_items(sid)
    if plan_guard.open_items(existing) and [i["text"] for i in existing] == texts:
        # the same plan again resets it and gives the model nothing new, so a weak model can re-plan forever
        cur = plan_guard.current_item(existing)
        raise ValueError("this exact plan already exists - do not call create_plan again. Start doing step #"
                         + str(cur["ord"] if cur else 1) + " now with a real action (write_file, edit_file, run_shell), "
                         "not more research. In Plan mode (read-only) there is nothing to act with: write the plan out as text and stop.")
    if (plan_guard.open_items(existing) and any(i["status"] in ("done", "failed") for i in existing)
            and not args.get("replace")):
        raise ValueError("a plan is already in progress and some steps are finished. Do not recreate it: continue "
                         "with the current step, or pass replace=true if the task really changed.")
    db_set_plan_items(sid, texts)
    db_set_plan_item_status(sid, 1, "in_progress")          # work starts on step 1
    return (f"Plan created with {len(texts)} steps. Work on step #1 now; mark it done before starting #2.\n"
            + _format_plan(db_get_plan_items(sid)))


def tool_update_plan_item(args: dict) -> str:
    from ..db import db_get_plan_items, db_set_plan_item_status
    if get_plan_context() is None:
        raise ValueError("no active session for plan tracking (session_id missing from /agent/run)")
    items = db_get_plan_items(get_plan_context())
    if not items:
        raise ValueError("no plan exists yet — call create_plan first")
    try:
        no = int(args.get("item", 0))
    except (TypeError, ValueError):
        raise ValueError("'item' must be the 1-based step number")
    status = str(args.get("status") or "done").strip().lower()
    if status not in _PLAN_STATUS_MARKS:
        raise ValueError("status must be one of: pending, in_progress, done, failed")
    if not 1 <= no <= len(items):
        raise ValueError(f"item {no} out of range (plan has {len(items)} steps)")
    from ..agent_loop import plan_guard
    err = plan_guard.check_update(items, no, status)
    if err:
        raise ValueError(err)
    sid = get_plan_context()
    db_set_plan_item_status(sid, no, status, str(args.get("note") or "").strip() or None)
    out = f"Step {no} marked {status} {_PLAN_STATUS_MARKS[status]}.\n"
    if status in ("done", "failed"):
        after = db_get_plan_items(sid)
        if not any(i["status"] == "in_progress" for i in after):
            nxt = next((i for i in after if i["status"] == "pending"), None)
            if nxt:
                db_set_plan_item_status(sid, nxt["ord"], "in_progress")
                out += f"Next: step #{nxt['ord']} - {nxt['text']}\n"
            else:
                out += "All steps are finished. Give the final answer.\n"
    return out + _format_plan(db_get_plan_items(sid))


def tool_get_plan(args: dict) -> str:
    from ..db import db_get_plan_items
    if get_plan_context() is None:
        raise ValueError("no active session for plan tracking (session_id missing from /agent/run)")
    return _format_plan(db_get_plan_items(get_plan_context()))
