"""
routes/knowledge.py - organizational knowledge base: ingestion, role-access,
delete, reindex. All management endpoints require knowledge.manage; retrieval
itself is wired into routes/chat.py and routes/agent.py via
core.knowledge_access.allowed_source_ids_for() + core.memory.search_memory_hybrid().
"""

import hashlib
import mimetypes
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File as FastAPIFile, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from core import auth_db
from core import knowledge_ingest
from core.audit import audit_log
from core.auth import Principal
from core.config import KNOWLEDGE_UPLOADS_DIR
from core.deps import get_current_user, require_permission
from core.memory import index_knowledge_source, delete_knowledge_chunks

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


def _source_public(row) -> dict:
    return {
        "id": row["id"], "title": row["title"], "kind": row["kind"], "origin": row["origin"],
        "status": row["status"], "error": row["error"], "created_by": row["created_by"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "roles": auth_db.get_source_role_names(row["id"]),
    }


async def _finish_ingest(source_id: int, text: str, user: Principal) -> dict:
    try:
        knowledge_ingest.reject_if_pan(text)
    except ValueError as e:
        auth_db.update_knowledge_source_status(source_id, "error", str(e))
        audit_log(user, action="knowledge.ingest", resource=str(source_id), result="deny",
                  detail={"reason": "pan_detected"})
        return {"ok": False, "error": str(e)}
    if not text.strip():
        auth_db.update_knowledge_source_status(source_id, "error", "no extractable text")
        return {"ok": False, "error": "no extractable text found"}
    n_chunks = await index_knowledge_source(source_id, text)
    auth_db.update_knowledge_source_status(source_id, "ready")
    audit_log(user, action="knowledge.ingest", resource=str(source_id), result="allow",
              detail={"chunks": n_chunks})
    return {"ok": True, "chunks": n_chunks}


@router.get("")
async def list_sources(user: Principal = Depends(require_permission("knowledge.manage"))):
    return {"sources": [_source_public(r) for r in auth_db.list_knowledge_sources()]}


@router.post("/text")
async def add_text_source(title: str, text: str,
                           user: Principal = Depends(require_permission("knowledge.manage"))):
    source_id = auth_db.create_knowledge_source(title=title, kind="text", created_by=user.id)
    result = await _finish_ingest(source_id, text, user)
    return {**result, "source": _source_public(auth_db.get_knowledge_source(source_id))}


class UrlBody(BaseModel):
    title: str
    url: str


@router.post("/url")
async def add_url_source(body: UrlBody, user: Principal = Depends(require_permission("knowledge.manage"))):
    source_id = auth_db.create_knowledge_source(title=body.title, kind="url", origin=body.url, created_by=user.id)
    try:
        text = await knowledge_ingest.extract_url(body.url)
    except Exception as e:
        auth_db.update_knowledge_source_status(source_id, "error", str(e))
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    result = await _finish_ingest(source_id, text, user)
    return {**result, "source": _source_public(auth_db.get_knowledge_source(source_id))}


@router.post("/upload")
async def upload_source(file: UploadFile = FastAPIFile(...), title: Optional[str] = None,
                         user: Principal = Depends(require_permission("knowledge.manage"))):
    fname = Path(file.filename or "upload").name
    ext = Path(fname).suffix.lower()
    if ext not in knowledge_ingest.EXTRACTORS:
        return JSONResponse({"error": f"unsupported file type: {ext}"}, status_code=400)
    KNOWLEDGE_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    data = await file.read()
    content_hash = hashlib.sha256(data).hexdigest()
    stored_name = f"{uuid.uuid4().hex}{ext}"
    dest = KNOWLEDGE_UPLOADS_DIR / stored_name
    dest.write_bytes(data)
    source_id = auth_db.create_knowledge_source(
        title=title or fname, kind="file", origin=fname, stored_path=str(dest),
        content_hash=content_hash, created_by=user.id,
    )
    try:
        text = knowledge_ingest.extract_file(dest)
    except Exception as e:
        auth_db.update_knowledge_source_status(source_id, "error", str(e))
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    result = await _finish_ingest(source_id, text, user)
    return {**result, "source": _source_public(auth_db.get_knowledge_source(source_id))}


class RoleAccessBody(BaseModel):
    roles: list[str]


@router.put("/{source_id}/access")
async def set_access(source_id: int, body: RoleAccessBody,
                      user: Principal = Depends(require_permission("knowledge.manage"))):
    if not auth_db.get_knowledge_source(source_id):
        return JSONResponse({"error": "source not found"}, status_code=404)
    auth_db.set_source_role_access(source_id, body.roles, granted_by=user.id)
    audit_log(user, action="knowledge.access", resource=str(source_id), detail={"roles": body.roles}, result="allow")
    return {"ok": True, "source": _source_public(auth_db.get_knowledge_source(source_id))}


@router.get("/{source_id}/file")
async def get_source_file(source_id: int, user: Principal = Depends(require_permission("knowledge.manage"))):
    """Serve the original uploaded file for preview/download in the admin UI."""
    row = auth_db.get_knowledge_source(source_id)
    if not row or row["kind"] != "file" or not row["stored_path"]:
        return JSONResponse({"error": "no file for this source"}, status_code=404)
    p = Path(row["stored_path"])
    if not p.exists():
        return JSONResponse({"error": "file missing on disk"}, status_code=404)
    media_type = mimetypes.guess_type(row["origin"] or p.name)[0] or "application/octet-stream"
    return FileResponse(str(p), media_type=media_type, filename=row["origin"] or p.name,
                         content_disposition_type="inline")


@router.post("/{source_id}/reindex")
async def reindex_source(source_id: int, user: Principal = Depends(require_permission("knowledge.manage"))):
    row = auth_db.get_knowledge_source(source_id)
    if not row:
        return JSONResponse({"error": "source not found"}, status_code=404)
    if row["kind"] == "file" and row["stored_path"] and Path(row["stored_path"]).exists():
        try:
            text = knowledge_ingest.extract_file(Path(row["stored_path"]))
        except Exception as e:
            auth_db.update_knowledge_source_status(source_id, "error", str(e))
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    elif row["kind"] == "url" and row["origin"]:
        try:
            text = await knowledge_ingest.extract_url(row["origin"])
        except Exception as e:
            auth_db.update_knowledge_source_status(source_id, "error", str(e))
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    else:
        return JSONResponse({"error": "this source kind cannot be reindexed automatically (re-add pasted text instead)"}, status_code=400)
    result = await _finish_ingest(source_id, text, user)
    return {**result, "source": _source_public(auth_db.get_knowledge_source(source_id))}


@router.delete("/{source_id}")
async def delete_source(source_id: int, user: Principal = Depends(require_permission("knowledge.manage"))):
    row = auth_db.get_knowledge_source(source_id)
    if not row:
        return JSONResponse({"error": "source not found"}, status_code=404)
    if row["kind"] == "file" and row["stored_path"]:
        try:
            Path(row["stored_path"]).unlink(missing_ok=True)
        except Exception:
            pass
    delete_knowledge_chunks(source_id)
    auth_db.delete_knowledge_source(source_id)
    audit_log(user, action="knowledge.delete", resource=row["title"], result="allow")
    return {"ok": True}
