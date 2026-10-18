import asyncio
import shutil
import sys
import time
from core import auth_db
from core.audit import audit_log
from core.auth import Principal
from core.memory import index_knowledge_source, kb_chunk_count, kb_chunk_report, kb_total_chunk_limit
from .models import CHUNKS_TEMP_DIR


def _source_public(row) -> dict:
    from core import kb_queue
    return {
        "progress": kb_queue.progress(row["id"]),
        "id": row["id"],
        "title": row["title"],
        "kind": row["kind"],
        "origin": row["origin"],
        "status": row["status"],
        "error": row["error"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "chunks_indexed": row["chunks_indexed"] if "chunks_indexed" in row.keys() else None,
        "chunks_total": row["chunks_total"] if "chunks_total" in row.keys() else None,
        "partial": bool(row["chunks_total"] and row["chunks_indexed"] is not None
                        and row["chunks_indexed"] < row["chunks_total"]) if "chunks_total" in row.keys() else False,
        "roles": auth_db.get_source_role_names(row["id"]),
        "cloud_ok": bool(row["cloud_ok"]),
        "category": row["category"],
        "cloud_effective": row["id"] in auth_db.cloud_ok_source_ids(),
    }


async def _finish_ingest(source_id: int, text: str, user: Principal, progress=None) -> dict:
    """Store table rows, chunk and embed one source (verbatim, no guardrail at upload). With `progress` (the background queue) the CPU-heavy
    steps run in a thread so a large file does not stall the server."""
    async def _run(fn, *a):
        return await asyncio.to_thread(fn, *a) if progress else fn(*a)

    if not text.strip():
        auth_db.update_knowledge_source_status(source_id, "error", "no extractable text")
        return {"ok": False, "error": "no extractable text found"}
    # No guardrail at upload (decision 2026-10-18): the document is stored and indexed exactly as
    # provided -- nothing is masked, blocked or rewritten here. The guard runs when the data is
    # ACCESSED: every KB read path masks card numbers on the way out (core/kb_guard.py).
    report = await _run(kb_chunk_report, text)
    limit, others = kb_total_chunk_limit(), kb_chunk_count(exclude_source=source_id)
    if others + report["indexed"] > limit:          # refuse before the in-memory search index outgrows the server
        msg = (f"knowledge index is full: {others:,} chunks stored + {report['indexed']:,} for this source exceeds "
               f"knowledge.max_total_chunks ({limit:,})")
        auth_db.update_knowledge_source_status(source_id, "error", msg)
        audit_log(user, action="knowledge.ingest", resource=str(source_id), result="deny", detail={"reason": "index_full"})
        return {"ok": False, "error": msg}
    try:                                    # Excel/CSV rows are also kept as real rows for exact queries (kb_query)
        from core import kb_tables
        await _run(kb_tables.replace_source, source_id, text)
    except Exception as e:
        print(f"[knowledge] could not store table rows for source {source_id}: {e}", file=sys.stderr)
    n_chunks = (await index_knowledge_source(source_id, text, progress=progress) if progress
                else await index_knowledge_source(source_id, text))
    auth_db.update_knowledge_source_status(source_id, "ready")
    auth_db.update_knowledge_source_index(source_id, report["indexed"], report["total"])
    partial = report["indexed"] < report["total"]
    audit_log(user, action="knowledge.ingest", resource=str(source_id), result="allow",
              detail={"chunks": n_chunks, "partial": partial})
    return {"ok": True, "chunks": n_chunks, "chunks_total": report["total"], "partial": partial}


async def _queue_file(source_id: int, path, user: Principal) -> None:
    """Hand a stored file to the background queue (extraction happens on the worker, in a thread)."""
    from core import kb_queue, knowledge_ingest

    async def load():
        return await asyncio.to_thread(knowledge_ingest.extract_file, path)
    auth_db.update_knowledge_source_status(source_id, "processing")
    await kb_queue.enqueue(source_id, load, user)


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
