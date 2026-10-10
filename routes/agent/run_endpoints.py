"""Endpoints for runs that outlive their request (routes/agent/run_manager.py).

  GET  /agent/runs?session_id=N           this user's runs still held (running, or finished and not yet saved)
  GET  /agent/run/{id}/events?after=SEQ   replay from SEQ+1, then follow live (SSE, frames tagged `id:`)
  POST /agent/run/{id}/cancel             stop a run (the Stop button; closing a tab no longer does)
  POST /agent/run/{id}/ack                the client saved the result: the server may forget the run

Every endpoint is scoped to the run's owner; another user's run id is a 404, never a 403.
"""

from typing import Optional

from fastapi import Depends, Header, Query
from fastapi.responses import JSONResponse, StreamingResponse

from core.audit import audit_log
from core.auth import Principal
from core.deps import get_current_user

from . import run_manager
from .base import router


def _not_found():
    return JSONResponse({"error": "run not found (it may have finished and been saved, or the server restarted)"},
                        status_code=404)


@router.get("/agent/runs")
async def agent_runs(session_id: Optional[int] = None, user: Principal = Depends(get_current_user)):
    return {"runs": run_manager.list_for(user.id, session_id)}


@router.get("/agent/run/{run_id}/events")
async def agent_run_events(run_id: str, after: int = Query(-1), last_event_id: Optional[str] = Header(None),
                           user: Principal = Depends(get_current_user)):
    handle = run_manager.owned(run_id, user.id)
    if handle is None:
        return _not_found()
    if after < 0 and last_event_id and last_event_id.lstrip("-").isdigit():
        after = int(last_event_id)               # EventSource-style resume
    return StreamingResponse(run_manager.subscribe(handle, after), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/agent/run/{run_id}/cancel")
async def agent_run_cancel(run_id: str, user: Principal = Depends(get_current_user)):
    handle = run_manager.owned(run_id, user.id)
    if handle is None:
        return _not_found()
    cancelled = handle.cancel()
    if cancelled:
        handle.acked = True          # the user stopped it: what the client saved is the result, nothing to replay
        audit_log(user, action="agent.run.cancel", resource=run_id, detail={"session_id": handle.session_id})
    return {"ok": True, "cancelled": cancelled, "state": handle.state}


@router.post("/agent/run/{run_id}/ack")
async def agent_run_ack(run_id: str, user: Principal = Depends(get_current_user)):
    handle = run_manager.owned(run_id, user.id)
    if handle is None:
        return {"ok": True, "known": False}      # already forgotten: acknowledging twice is fine
    if handle.done:
        handle.acked = True
    return {"ok": True, "known": True, "running": not handle.done}
