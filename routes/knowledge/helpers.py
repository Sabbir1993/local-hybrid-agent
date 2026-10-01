import shutil
import time
from core import auth_db, pan
from core.audit import audit_log
from core.auth import Principal
from core.memory import index_knowledge_source
from .models import CHUNKS_TEMP_DIR


def _source_public(row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "kind": row["kind"],
        "origin": row["origin"],
        "status": row["status"],
        "error": row["error"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "roles": auth_db.get_source_role_names(row["id"]),
    }


async def _finish_ingest(source_id: int, text: str, user: Principal) -> dict:
    if not text.strip():
        auth_db.update_knowledge_source_status(source_id, "error", "no extractable text")
        return {"ok": False, "error": "no extractable text found"}
    # Mask payment card numbers to their last 4 digits before anything is chunked and
    # embedded. Rejecting the upload would make a document that legitimately discusses card
    # handling impossible to ingest, and masking already closes the path that matters: a PAN
    # in the vector store can be retrieved into a prompt and sent to a cloud lane, a masked
    # one cannot. Uses core/pan.py, the same detector as chat input, model output and cloud
    # egress -- the count is audited so the uploading admin can see that redaction happened.
    text, n_pans = pan.mask_pans(text)
    n_chunks = await index_knowledge_source(source_id, text)
    auth_db.update_knowledge_source_status(source_id, "ready")
    audit_log(user, action="knowledge.ingest", resource=str(source_id), result="allow",
              detail={"chunks": n_chunks, "pans_masked": n_pans})
    return {"ok": True, "chunks": n_chunks, "pans_masked": n_pans}


def _cleanup_old_chunks(max_age_s: float = 86400.0) -> None:
    if not CHUNKS_TEMP_DIR.exists():
        return
    now = time.time()
    for item in CHUNKS_TEMP_DIR.iterdir():
        try:
            if item.is_dir() and (now - item.stat().st_mtime) > max_age_s:
                shutil.rmtree(item, ignore_errors=True)
        except Exception:
            pass
