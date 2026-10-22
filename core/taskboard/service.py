import time
from typing import Optional

from .. import runs

TITLES = {"chat": "Chat reply", "agent": "Agent run", "subagent": "Sub-agent", "deck": "Slide deck",
          "kb_ingest": "Knowledge ingest", "media": "Media job"}
ACTIVE = "running"


def _agent_handle(user_id: int, run_id: str):
    from routes.agent import run_manager
    return run_manager.owned(run_id, user_id)


def _media_job(user_id: int, run_id: str):
    if not run_id.startswith("media-"):
        return None
    from ..media import jobs
    j = jobs.get_job(run_id[len("media-"):], user_id)
    return j if j is not None and not j.done else None


def _record(row: dict, user_id: int) -> dict:
    run_id, kind, state = row["run_id"], row["kind"], row["state"]
    live = state == ACTIVE
    handle = _agent_handle(user_id, run_id) if live and kind == "agent" else None
    can_stop = bool(handle is not None and not handle.done) or (live and kind == "media" and _media_job(user_id, run_id) is not None)
    title = TITLES.get(kind, kind)
    if row.get("ref") and kind in ("media", "kb_ingest"):
        title += f" - {row['ref']}"
    return {"id": run_id, "kind": kind, "title": title, "state": state, "stage": row.get("stage"),
            "step": row.get("step") or 0, "reason": row.get("reason"), "session_id": row.get("session_id"),
            "parent_id": row.get("parent_id"), "created_at": row.get("created_at"), "updated_at": row.get("updated_at"),
            "age_s": round(max(0.0, time.time() - (row.get("created_at") or time.time())), 1),
            "resumable": bool(row.get("resumable")), "can_stop": can_stop,
            "can_attach": bool(handle is not None and not handle.done),
            "attach": {"events": f"/agent/run/{run_id}/events"} if handle is not None and not handle.done else None}


def list_tasks(user_id: int, kind: Optional[str] = None, state: Optional[str] = None, limit: int = 50) -> list:
    """`state` is a journal state, or 'active' for what is running now."""
    state = ACTIVE if state == "active" else state
    return [_record(r, user_id) for r in runs.recent(user_id, kind or None, state or None, limit)]


def get_task(user_id: int, task_id: str) -> Optional[dict]:
    row = runs.get(task_id)
    return _record(row, user_id) if row and row.get("user_id") == user_id else None   # someone else's id looks unknown


def stop_task(user_id: int, task_id: str) -> Optional[dict]:
    """None when the task is not this user's. Otherwise {ok, stopped, note}."""
    task = get_task(user_id, task_id)
    if task is None:
        return None
    if not task["can_stop"]:
        why = "already finished" if task["state"] != ACTIVE else f"a {task['kind']} task cannot be stopped from here"
        return {"ok": True, "stopped": False, "note": why, "task": task}
    if task["kind"] == "agent":
        handle = _agent_handle(user_id, task_id)
        stopped = bool(handle and handle.cancel())
        if stopped:
            handle.acked = True      # the user stopped it: what the client saved is the result
    else:
        from ..media import jobs
        stopped = jobs.cancel_job(task_id[len("media-"):], user_id)
    return {"ok": True, "stopped": stopped, "note": "stop requested" if stopped else "nothing to stop", "task": get_task(user_id, task_id)}
