import hashlib
import shutil
import uuid
from pathlib import Path
from fastapi import APIRouter, Depends, File as FastAPIFile, Form, UploadFile
from fastapi.responses import JSONResponse
from core import auth_db, knowledge_ingest
from core.auth import Principal
from core.config import KNOWLEDGE_UPLOADS_DIR
from core.deps import require_permission
from .helpers import _cleanup_old_chunks, _finish_ingest, _source_public
from .models import (
    CHUNKS_TEMP_DIR,
    MAX_KB_CHUNK_BYTES,
    MAX_KB_CHUNKS,
    CompleteUploadBody,
)

router = APIRouter()


@router.post("/upload/chunk")
async def upload_chunk(
    upload_id: str = Form(...),
    chunk_index: int = Form(...),
    total_chunks: int = Form(...),
    file: UploadFile = FastAPIFile(...),
    user: Principal = Depends(require_permission("knowledge.manage")),
):
    """Receive a single chunk of a large file upload."""
    if not upload_id or not all(c.isalnum() or c in "-_" for c in upload_id):
        return JSONResponse({"error": "invalid upload_id"}, status_code=400)
    if chunk_index < 0 or total_chunks <= 0 or chunk_index >= total_chunks or total_chunks > MAX_KB_CHUNKS:
        return JSONResponse({"error": "invalid chunk_index or total_chunks"}, status_code=400)

    _cleanup_old_chunks()
    upload_dir = CHUNKS_TEMP_DIR / upload_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    chunk_path = upload_dir / f"{chunk_index}.part"

    chunk_data = await file.read(MAX_KB_CHUNK_BYTES + 1)
    if len(chunk_data) > MAX_KB_CHUNK_BYTES:
        return JSONResponse({"error": "chunk too large"}, status_code=413)
    chunk_path.write_bytes(chunk_data)
    return {"ok": True, "chunk_index": chunk_index, "total_chunks": total_chunks}


@router.post("/upload/complete")
async def complete_chunked_upload(
    body: CompleteUploadBody,
    user: Principal = Depends(require_permission("knowledge.manage")),
):
    """Assemble chunks of a large file, extract text, and index into the knowledge base."""
    upload_id = body.upload_id
    if not upload_id or not all(c.isalnum() or c in "-_" for c in upload_id):
        return JSONResponse({"error": "invalid upload_id"}, status_code=400)

    upload_dir = CHUNKS_TEMP_DIR / upload_id
    if not upload_dir.exists():
        return JSONResponse({"error": "upload session not found or expired"}, status_code=404)

    fname = Path(body.filename or "upload").name
    ext = Path(fname).suffix.lower()
    if ext not in knowledge_ingest.EXTRACTORS:
        shutil.rmtree(upload_dir, ignore_errors=True)
        return JSONResponse({"error": f"unsupported file type: {ext}"}, status_code=400)

    for idx in range(body.total_chunks):
        part_path = upload_dir / f"{idx}.part"
        if not part_path.exists():
            return JSONResponse({"error": f"missing chunk {idx} of {body.total_chunks}"}, status_code=400)

    KNOWLEDGE_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex}{ext}"
    dest = KNOWLEDGE_UPLOADS_DIR / stored_name

    hasher = hashlib.sha256()
    with open(dest, "wb") as out_f:
        for idx in range(body.total_chunks):
            part_path = upload_dir / f"{idx}.part"
            chunk_bytes = part_path.read_bytes()
            hasher.update(chunk_bytes)
            out_f.write(chunk_bytes)

    shutil.rmtree(upload_dir, ignore_errors=True)
    content_hash = hasher.hexdigest()

    source_id = auth_db.create_knowledge_source(
        title=body.title or fname, kind="file", origin=fname, stored_path=str(dest),
        content_hash=content_hash, status="processing", created_by=user.id,
    )
    try:
        text = knowledge_ingest.extract_file(dest)
    except Exception as e:
        auth_db.update_knowledge_source_status(source_id, "error", str(e))
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    result = await _finish_ingest(source_id, text, user)
    return {**result, "source": _source_public(auth_db.get_knowledge_source(source_id))}
