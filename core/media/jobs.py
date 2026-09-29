import asyncio
import time
from datetime import datetime
from typing import Optional

from .constants import KINDS, MediaError, NotLoadedError, media_cfg, plain_error


class Job:
    def __init__(self, user, kind: str, prompt: str, opts: dict, lane: Optional[str]):
        import uuid
        self.id = uuid.uuid4().hex[:16]
        self.user = user
        self.user_id = user.id
        self.kind = kind
        self.prompt = prompt
        self.opts = opts
        self.lane = lane
        self.created = time.time()
        self.events: list = []
        self.done = False
        self.changed = asyncio.Event()
        self.task: Optional[asyncio.Task] = None

    def push(self, ev: str, data: dict) -> None:
        self.events.append((ev, data))
        self.changed.set()


_JOBS: dict = {}
_KEEP_S = 30 * 60
_MAX_RUNNING = {"image": 2, "video": 1}


def _gc() -> None:
    now = time.time()
    for jid, j in list(_JOBS.items()):
        if j.done and now - j.created > _KEEP_S:
            _JOBS.pop(jid, None)


def running(user_id: int, kind: str) -> int:
    return sum(1 for j in _JOBS.values() if j.user_id == user_id and j.kind == kind and not j.done)


def used_today(user_id: int, kind: str) -> int:
    """Cloud generations this user made today (from the audit log)."""
    try:
        from ..auth_db import db
        start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        row = db().execute(
            "SELECT COUNT(*) FROM audit_log WHERE user_id=? AND action=? AND resource='cloud' "
            "AND result='allow' AND ts>=?", (user_id, f"media.{kind}", start)).fetchone()
        return int(row[0] or 0)
    except Exception:
        return 0


def daily_limit(kind: str) -> int:
    lim = media_cfg().get("limits") or {}
    try:
        return int(lim.get(f"{kind}_per_day", 50 if kind == "image" else 5))
    except (TypeError, ValueError):
        return 0


import sys

def _get_used_today(user_id: int, kind: str) -> int:
    mod = sys.modules.get("core.media")
    fn = getattr(mod, "used_today", used_today) if mod else used_today
    return fn(user_id, kind)


def check_cloud_quota(user_id: int, kind: str) -> None:
    lim = daily_limit(kind)
    if lim > 0 and _get_used_today(user_id, kind) >= lim:
        raise MediaError(f"Daily limit reached for cloud {kind}s ({lim}). It resets at midnight.")


async def _run(job: Job) -> None:
    from ..audit import audit_log
    from .service import generate
    mod = sys.modules.get("core.media")
    gen_fn = getattr(mod, "generate", generate) if mod else generate
    t0 = time.time()

    async def _progress(text: str, pct: Optional[int]) -> None:
        job.push("progress", {"text": text, "pct": pct, "elapsed": int(time.time() - t0)})

    pics = job.opts.get("inputs") or []
    pic_meta = ({"mode": job.opts.get("mode"), "inputs": len(pics),
                 "input_bytes": sum(len(p.get("png") or b"") for p in pics)} if pics else {})
    try:
        res = await gen_fn(job.kind, job.user, job.prompt, job.opts, _progress, job.lane)
        audit_log(job.user, action=f"media.{job.kind}", resource=res["source"],
                  detail={"prompt_chars": len(job.prompt), "lane": res["lane"], "ms": res["ms"], **pic_meta},
                  result="allow")
        job.push("done", res)
    except asyncio.CancelledError:
        job.push("error", {"message": "Cancelled.", "cancelled": True})
    except NotLoadedError as e:
        job.push("error", {"message": str(e), "not_loaded": {"lane": e.lane, "label": e.label}})
    except Exception as e:
        audit_log(job.user, action=f"media.{job.kind}", resource="failed",
                  detail={"prompt_chars": len(job.prompt)}, result="error")
        job.push("error", {"message": plain_error(e)})
    finally:
        job.opts.pop("inputs", None)
        job.done = True
        job.changed.set()


def start_job(user, kind: str, prompt: str, opts: dict, lane: Optional[str] = None) -> Job:
    _gc()
    if kind not in KINDS:
        raise MediaError("kind must be image or video")
    if running(user.id, kind) >= _MAX_RUNNING[kind]:
        raise MediaError(f"You already have {'a video' if kind == 'video' else 'two images'} in progress - "
                         "wait for it to finish or cancel it.")
    job = Job(user, kind, prompt, opts, lane)
    _JOBS[job.id] = job
    job.push("queued", {"kind": kind})
    job.task = asyncio.create_task(_run(job))
    return job


def get_job(job_id: str, user_id: int) -> Optional[Job]:
    j = _JOBS.get(job_id)
    return j if j and j.user_id == user_id else None


def cancel_job(job_id: str, user_id: int) -> bool:
    j = get_job(job_id, user_id)
    if not j or j.done or not j.task:
        return False
    j.task.cancel()
    return True


async def job_events(job: Job, heartbeat_s: float = 15.0):
    i = 0
    while True:
        while i < len(job.events):
            yield job.events[i]
            i += 1
        if job.done:
            return
        job.changed.clear()
        try:
            await asyncio.wait_for(job.changed.wait(), timeout=heartbeat_s)
        except asyncio.TimeoutError:
            yield ("ping", {"elapsed": int(time.time() - job.created)})


async def run_to_end(job: Job) -> dict:
    try:
        async for ev, data in job_events(job):
            if ev == "done":
                return data
            if ev == "error":
                raise MediaError(data.get("message") or "failed")
    except asyncio.CancelledError:
        if job.task and not job.done:
            job.task.cancel()
        raise
    raise MediaError("the job ended without a result")
