"""routes/taskboard - what is running in the background, and stop.

  GET  /tasks?kind=&state=&limit=   this user's tasks, running first ('state=active' = running now)
  GET  /tasks/{id}                  one task
  POST /tasks/{id}/stop             stop it (agent runs and media jobs; others report why not)

Read from the run journal, scoped to the signed-in user: another user's id is a 404, never a 403.
Attaching to a running agent task is the existing GET /agent/run/{id}/events (the record carries the URL).
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from core import taskboard
from core.audit import audit_log
from core.auth import Principal
from core.deps import get_current_user

router = APIRouter(tags=["taskboard"])


def _not_found():
    return JSONResponse({"error": "task not found"}, status_code=404)


@router.get("/tasks")
async def tasks_list(kind: Optional[str] = None, state: Optional[str] = None, limit: int = Query(50, ge=1, le=200),
                     user: Principal = Depends(get_current_user)):
    return {"tasks": taskboard.list_tasks(user.id, kind, state, limit)}


@router.get("/tasks/{task_id}")
async def task_get(task_id: str, user: Principal = Depends(get_current_user)):
    t = taskboard.get_task(user.id, task_id)
    return t if t is not None else _not_found()


@router.post("/tasks/{task_id}/stop")
async def task_stop(task_id: str, user: Principal = Depends(get_current_user)):
    r = taskboard.stop_task(user.id, task_id)
    if r is None:
        return _not_found()
    if r["stopped"]:
        audit_log(user, action="task.stop", resource=task_id, detail={"kind": r["task"]["kind"] if r.get("task") else None})
    return r
