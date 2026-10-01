"""The signed-in user's own long-term agent memory: list, read, edit, delete (Settings -> Agent memory).

Everything here is scoped to the caller. Edits made here go through the same secret filter, size
cap and version check as the agent's own memory tools, and are audited by path (never content).
"""
from typing import Optional

from fastapi import Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core import agent_memory
from core.auth import Principal
from core.deps import get_current_user

from .base import router


class MemoryWriteReq(BaseModel):
    content: str
    description: Optional[str] = None
    if_version: Optional[str] = None


def _public(f: dict, with_text: bool = False) -> dict:
    out = {"path": f["path"], "description": f["description"], "bytes": f["bytes"],
           "version": f["version"], "updated_at": f["updated_at"]}
    if with_text:
        out["body"] = f["body"]
        out["text"] = f["text"]
    return out


@router.get("/agent/memory")
async def memory_list(user: Principal = Depends(get_current_user)):
    files = agent_memory.list_files(user.id)
    return {"files": [_public(f) for f in files],
            "limits": {"file_max_bytes": agent_memory.memory_limit("file_max_bytes"),
                       "max_files": agent_memory.memory_limit("max_files")}}


@router.get("/agent/memory/{path:path}")
async def memory_get(path: str, user: Principal = Depends(get_current_user)):
    try:
        f = agent_memory.read(user.id, path)
    except agent_memory.MemoryError_ as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    if not f:
        return JSONResponse({"error": "not found"}, status_code=404)
    return _public(f, with_text=True)


@router.put("/agent/memory/{path:path}")
async def memory_put(path: str, req: MemoryWriteReq, user: Principal = Depends(get_current_user)):
    try:
        f = agent_memory.write(user.id, path, req.content, req.if_version, req.description)
    except agent_memory.MemoryError_ as e:
        code = 409 if str(e).startswith("version conflict") else 400
        return JSONResponse({"error": str(e)}, status_code=code)
    return _public(f, with_text=True)


@router.delete("/agent/memory/{path:path}")
async def memory_remove(path: str, user: Principal = Depends(get_current_user)):
    try:
        gone = agent_memory.delete(user.id, path)
    except agent_memory.MemoryError_ as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    if not gone:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"ok": True}


@router.delete("/agent/memory")
async def memory_remove_all(user: Principal = Depends(get_current_user)):
    return {"ok": True, "deleted": agent_memory.delete_all(user.id)}
