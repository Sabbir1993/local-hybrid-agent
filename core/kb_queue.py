"""Background ingestion for large knowledge-base files.

Extracting and embedding a 50 MB PDF or workbook takes minutes. Doing it inside the upload request ties up the
request (and, for the CPU-heavy steps, the server's event loop) and gives the admin nothing to look at. Large
files are queued instead: the upload returns at once with the source in status "processing", one worker handles
the queue in order, and `progress(source_id)` says what it is doing. Small files still ingest inline.

One worker, one job at a time, on purpose: embedding runs on the GPU the models share, and a serial queue keeps
memory use and cache rebuilds predictable.
"""

import asyncio
import sys
import time
from typing import Awaitable, Callable, Optional

DEFAULT_BACKGROUND_MIN_BYTES = 2 * 1024 * 1024
ORPHAN_AFTER_S = 900

_jobs: dict = {}                      # source_id -> {"state", "done", "total", "since"}
_queue: Optional[asyncio.Queue] = None
_worker: Optional[asyncio.Task] = None
_loop = None


def background_min_bytes() -> int:
    try:
        from .small_model import APP_CONFIG
        return int((APP_CONFIG.get("knowledge") or {}).get("background_min_bytes", DEFAULT_BACKGROUND_MIN_BYTES))
    except Exception:
        return DEFAULT_BACKGROUND_MIN_BYTES


def active() -> bool:
    """True while any queued source is still being processed."""
    return any(j["state"] in ("queued", "extracting", "indexing") for j in _jobs.values())


def progress(source_id: int) -> Optional[dict]:
    j = _jobs.get(source_id)
    return dict(j) if j else None


def snapshot() -> dict:
    counts = {}
    for j in _jobs.values():
        counts[j["state"]] = counts.get(j["state"], 0) + 1
    return {"jobs": len(_jobs), **counts}


def sweep_orphans() -> int:
    """A source left in "processing" with no job (the server restarted mid-ingest) would show a spinner forever."""
    from . import auth_db
    n = 0
    now = time.time()
    for row in auth_db.list_knowledge_sources():
        if row["status"] == "processing" and row["id"] not in _jobs and now - (row["updated_at"] or 0) > ORPHAN_AFTER_S:
            auth_db.update_knowledge_source_status(row["id"], "error", "ingest was interrupted (server restarted) - reindex it")
            n += 1
    return n


def _ensure_worker() -> asyncio.Queue:
    global _queue, _worker, _loop
    loop = asyncio.get_running_loop()
    if _queue is None or _loop is not loop or _worker is None or _worker.done():
        _loop = loop
        _queue = asyncio.Queue()
        _worker = loop.create_task(_run())
    return _queue


async def enqueue(source_id: int, load_text: Callable[[], Awaitable[str]], user) -> None:
    """Queue a source. `load_text` is an async callable returning its extracted text (run on the worker)."""
    q = _ensure_worker()
    _jobs[source_id] = {"state": "queued", "done": 0, "total": 0, "since": time.time()}
    await q.put((source_id, load_text, user))


async def _run() -> None:
    from . import auth_db
    from routes.knowledge.helpers import _finish_ingest
    while True:
        source_id, load_text, user = await _queue.get()
        job = _jobs.setdefault(source_id, {"state": "queued", "done": 0, "total": 0, "since": time.time()})
        try:
            job["state"] = "extracting"
            text = await load_text()

            def on_progress(done: int, total: int, job=job) -> None:
                job["state"], job["done"], job["total"] = "indexing", done, total
            job["state"] = "indexing"
            res = await _finish_ingest(source_id, text, user, progress=on_progress)
            job["state"] = "done" if res.get("ok") else "error"
        except asyncio.CancelledError:
            raise
        except Exception as e:                                   # one bad file must not stop the queue
            print(f"[kb_queue] source {source_id} failed: {type(e).__name__}: {e}", file=sys.stderr)
            job["state"] = "error"
            try:
                auth_db.update_knowledge_source_status(source_id, "error", f"{type(e).__name__}: {str(e)[:300]}")
            except Exception:
                pass
        finally:
            _queue.task_done()
            job["finished"] = time.time()
            _prune()


def _prune(keep: int = 200) -> None:
    """Forget finished jobs beyond the most recent `keep`; the database holds the lasting status."""
    done = [(j.get("finished", 0), sid) for sid, j in _jobs.items() if j["state"] in ("done", "error")]
    for _, sid in sorted(done)[:max(0, len(done) - keep)]:
        _jobs.pop(sid, None)
