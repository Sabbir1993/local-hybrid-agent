import shutil
import time
from core import auth_db, knowledge_ingest
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
