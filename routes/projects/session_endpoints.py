"""Session and message endpoints for projects."""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from core.auth import Principal
from core.db import (
    db_append_message,
    db_create_session,
    db_delete_session,
    db_list_sessions,
    db_load_messages,
    db_update_session_title,
)
from core.deps import get_current_user
from .models import MessageReq, SessionReq

router = APIRouter()


@router.get("/control/projects/{pid}/sessions")
async def list_sessions(pid: int, user: Principal = Depends(get_current_user)):
    try:
        return {"sessions": db_list_sessions(pid, owner_user_id=user.id)}
    except PermissionError:
        return JSONResponse({"error": "project not found"}, status_code=404)


@router.post("/control/projects/{pid}/sessions")
async def create_session(pid: int, req: SessionReq, user: Principal = Depends(get_current_user)):
    try:
        s = db_create_session(pid, req.title, owner_user_id=user.id)
    except PermissionError:
        return JSONResponse({"error": "project not found"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "session": s}


@router.patch("/control/sessions/{sid}")
async def update_session(sid: int, req: SessionReq, user: Principal = Depends(get_current_user)):
    try:
        db_update_session_title(sid, req.title, owner_user_id=user.id)
    except PermissionError:
        return JSONResponse({"error": "session not found"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True}


@router.delete("/control/sessions/{sid}")
async def delete_session(sid: int, user: Principal = Depends(get_current_user)):
    try:
        db_delete_session(sid, owner_user_id=user.id)
    except PermissionError:
        return JSONResponse({"error": "session not found"}, status_code=404)
    return {"ok": True}


@router.get("/control/sessions/{sid}/messages")
async def get_messages(sid: int, user: Principal = Depends(get_current_user)):
    try:
        return {"messages": db_load_messages(sid, owner_user_id=user.id)}
    except PermissionError:
        return JSONResponse({"error": "session not found"}, status_code=404)


@router.post("/control/sessions/{sid}/messages")
async def post_message(sid: int, req: MessageReq, user: Principal = Depends(get_current_user)):
    try:
        mid = db_append_message(sid, req.role, req.content, req.meta, owner_user_id=user.id)
    except PermissionError:
        return JSONResponse({"error": "session not found"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "id": mid}
