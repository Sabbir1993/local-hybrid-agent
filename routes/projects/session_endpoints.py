"""Session and message endpoints for projects."""

from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from core.auth import Principal
from core.db import (
    db_append_message,
    db_create_session,
    db_delete_session,
    db_fork_session,
    db_create_or_get_share,
    db_revoke_share,
    db_list_sessions_page,
    db_session_groups,
    db_load_messages,
    db_update_session_title,
    db_update_session_agent_id,
    db_update_session_model,
    db_update_session_pinned,
    db_search_sessions,
    _projects_db,
    _load_meta,
)
from core.deps import get_current_user
from .models import ForkReq, MessageReq, SessionReq, SessionMemoryReq

router = APIRouter()


@router.get("/control/session-groups")
async def session_groups(kind: str = "agent", ids: str = "", per: int = 5,
                         user: Principal = Depends(get_current_user)):
    """The sidebar tree in one request: each agent's (kind=agent) or project's (kind=project) newest sessions and
    their total. The client sends the ids it shows (this device's agents / projects); only the caller's rows count."""
    if kind not in ("agent", "project"):
        return JSONResponse({"error": "kind must be agent or project"}, status_code=400)
    group_ids = [x for x in (ids or "").split(",") if x.strip()]
    return {"groups": db_session_groups(user.id, kind, group_ids, per)}


@router.get("/control/projects/{pid}/sessions")
async def list_sessions(pid: int, limit: Optional[int] = None, before: Optional[int] = None,
                        user: Principal = Depends(get_current_user)):
    """Newest first. `limit` (1-100) + `before` (last id of the previous page) page the list; without them
    every session is returned."""
    if limit is not None:
        limit = max(1, min(int(limit), 100))
    try:
        rows, has_more = db_list_sessions_page(pid, owner_user_id=user.id, limit=limit, before_id=before)
        return {"sessions": rows, "has_more": has_more}
    except PermissionError:
        return JSONResponse({"error": "project not found"}, status_code=404)


@router.post("/control/projects/{pid}/sessions")
async def create_session(pid: int, req: SessionReq, user: Principal = Depends(get_current_user)):
    try:
        s = db_create_session(pid, req.title, owner_user_id=user.id, agent_id=req.agent_id, model=req.model)
    except PermissionError:
        return JSONResponse({"error": "project not found"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "session": s}


@router.get("/control/sessions/search")
async def search_sessions(q: str = "", user: Principal = Depends(get_current_user)):
    return {"sessions": db_search_sessions(user.id, q)}


@router.patch("/control/sessions/{sid}")
async def update_session(sid: int, req: SessionReq, user: Principal = Depends(get_current_user)):
    try:
        if req.title is not None:
            db_update_session_title(sid, req.title, owner_user_id=user.id)
        if req.agent_id is not None:
            db_update_session_agent_id(sid, req.agent_id, owner_user_id=user.id)
        if req.model is not None:
            db_update_session_model(sid, req.model, owner_user_id=user.id)
        if req.is_pinned is not None:
            db_update_session_pinned(sid, req.is_pinned, owner_user_id=user.id)
    except PermissionError:
        return JSONResponse({"error": "session not found"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True}


@router.delete("/control/sessions/{sid}")
async def delete_session(sid: int, user: Principal = Depends(get_current_user)):
    try:
        db_delete_session(sid, owner_user_id=user.id, is_super_admin=getattr(user, "is_super_admin", False))
    except PermissionError:
        return JSONResponse({"error": "session not found"}, status_code=404)
    return {"ok": True}


@router.get("/control/sessions/{sid}/messages")
async def get_messages(sid: int, user: Principal = Depends(get_current_user)):
    try:
        msgs = db_load_messages(sid, owner_user_id=user.id)
        return {"messages": msgs, "is_owner": True}
    except PermissionError:
        # Check if the session exists (e.g. shared link view for visitor)
        row = _projects_db.execute("SELECT id, user_id, title FROM sessions WHERE id = ?", (sid,)).fetchone()
        if row is not None:
            raw_msgs = [
                {"role": r["role"], "content": r["content"], "meta": _load_meta(r["meta"]), "created_at": r["created_at"]}
                for r in _projects_db.execute("SELECT * FROM messages WHERE session_id = ? ORDER BY id", (sid,))
            ]
            return {"messages": raw_msgs, "is_owner": False, "title": row["title"]}
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


@router.post("/control/sessions/{sid}/fork")
async def fork_session(sid: int, req: ForkReq, user: Principal = Depends(get_current_user)):
    """Copy this session (up to and including `upto_message_id`, or all of it) into a NEW
    session and return its id. Branch, never truncation: the original stays untouched.
    DEFAULT-OFF: sessions.fork_enabled in config/app.json - a 403 until it is armed, so
    the surface only appears when the operator deliberately turns it on."""
    from core.small_model import APP_CONFIG
    if not ((APP_CONFIG.get("sessions") or {}).get("fork_enabled")):
        return JSONResponse({"error": "session forking is disabled (config: sessions.fork_enabled)"},
                            status_code=403)
    try:
        out = db_fork_session(sid, owner_user_id=user.id, upto_message_id=req.upto_message_id)
    except PermissionError:
        return JSONResponse({"error": "session not found"}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "session": out}


@router.get("/control/sessions/{sid}/memory")
async def get_session_memory(sid: int, user: Principal = Depends(get_current_user)):
    from core.db import db_session_owner
    if db_session_owner(sid) != user.id:
        return JSONResponse({"error": "session not found"}, status_code=404)
    from core import working_memory
    content = working_memory.load(sid, user.id)
    return {"ok": True, "content": content}


@router.put("/control/sessions/{sid}/memory")
async def set_session_memory(sid: int, req: SessionMemoryReq, user: Principal = Depends(get_current_user)):
    from core.db import db_session_owner
    if db_session_owner(sid) != user.id:
        return JSONResponse({"error": "session not found"}, status_code=404)
    from core import working_memory
    working_memory.save(sid, user.id, req.content)
    return {"ok": True}


@router.post("/control/sessions/{sid}/share")
async def share_session(sid: int, user: Principal = Depends(get_current_user)):
    try:
        share_key = db_create_or_get_share(sid, user.id)
    except PermissionError:
        return JSONResponse({"error": "session not found"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "share_key": share_key, "share_url": f"/share/{share_key}"}


@router.delete("/control/sessions/{sid}/share")
async def unshare_session(sid: int, user: Principal = Depends(get_current_user)):
    try:
        revoked = db_revoke_share(sid, user.id)
    except PermissionError:
        return JSONResponse({"error": "session not found"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "revoked": revoked}
