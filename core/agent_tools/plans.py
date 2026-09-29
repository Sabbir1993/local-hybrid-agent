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
    from ..db import db_get_plan_items, db_set_plan_items
    if get_plan_context() is None:
        raise ValueError("no active session for plan tracking (session_id missing from /agent/run)")
    raw = args.get("items")
    if isinstance(raw, str):
        raw = [s.strip() for s in raw.replace(";", "\n").split("\n") if s.strip()]
    if not isinstance(raw, list) or not raw:
        raise ValueError("items must be a non-empty array of step description strings")
    texts = []
    for it in raw[:20]:
        t = str(it.get("text") or "").strip() if isinstance(it, dict) else str(it).strip()
        if t:
            texts.append(t)
    if not texts:
        raise ValueError("plan contained no usable step text")
    db_set_plan_items(get_plan_context(), texts)
    return f"Plan created with {len(texts)} steps:\n" + _format_plan(db_get_plan_items(get_plan_context()))


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
    db_set_plan_item_status(get_plan_context(), no, status, str(args.get("note") or "").strip() or None)
    return f"Step {no} marked {status} {_PLAN_STATUS_MARKS[status]}.\n" + _format_plan(db_get_plan_items(get_plan_context()))


def tool_get_plan(args: dict) -> str:
    from ..db import db_get_plan_items
    if get_plan_context() is None:
        raise ValueError("no active session for plan tracking (session_id missing from /agent/run)")
    return _format_plan(db_get_plan_items(get_plan_context()))
