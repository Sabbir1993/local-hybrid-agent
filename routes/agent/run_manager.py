"""routes/agent/run_manager.py - agent runs that outlive the HTTP request that started them.

The loop (routes/agent/stream.py) is an async generator of SSE frames. It used to run inside the response of
POST /agent/run, so closing the tab, reloading the page or losing the connection cancelled it. Now `start()` runs
that generator as a server-side task and the HTTP response is just one subscriber of its output:

  * a subscriber that goes away (tab closed, network drop) does not touch the run;
  * a new subscriber replays the buffered frames from any point (`after` = last seq seen) and then follows live;
  * only an explicit cancel stops a run (the user's Stop button, POST /agent/run/{id}/cancel).

Frames are held in memory only: a run's output holds code, command output and sometimes card numbers, so none of
it is written to disk here. The buffer is bounded per run, a finished run is kept for `run_retention_s` (or until
the client acknowledges it has saved the result) and a server restart drops everything, exactly as before.
Pings are not buffered: each subscriber emits its own keep-alives.
"""

import asyncio
import json
import re
import time
import uuid
from typing import AsyncIterator, Optional

from core.small_model import APP_CONFIG

PING_S = 15
DEFAULT_MAX_ACTIVE_PER_USER = 3
DEFAULT_RETENTION_S = 6 * 3600
DEFAULT_BUFFER_BYTES = 30 * 1024 * 1024
MAX_FRAMES = 40000
_ID_RX = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def _cfg(key: str, default):
    v = (APP_CONFIG.get("agent") or {}).get(key, default)
    return default if v is None else v


def enabled() -> bool:
    return bool(_cfg("detached_runs", True))


def valid_id(value) -> bool:
    return isinstance(value, str) and bool(_ID_RX.match(value))


class TooManyRuns(Exception):
    pass


class RunHandle:
    def __init__(self, run_id: str, user_id: int, session_id: Optional[int]):
        self.id = run_id
        self.user_id = user_id
        self.session_id = session_id
        self.started = time.time()
        self.finished_at: Optional[float] = None
        self.state = "running"          # running | completed | stopped | failed | cancelled | interrupted
        self.reason: Optional[str] = None
        self.frames: list = []
        self.base_seq = 0               # seq of frames[0]; grows when the buffer drops its oldest frames
        self.bytes = 0
        self.done = False
        self.cancel_requested = False
        self.acked = False
        self.task: Optional[asyncio.Task] = None
        self._cond = asyncio.Condition()

    @property
    def next_seq(self) -> int:
        return self.base_seq + len(self.frames)

    def summary(self) -> dict:
        return {"id": self.id, "session_id": self.session_id, "state": self.state, "reason": self.reason,
                "running": not self.done, "started": self.started, "finished_at": self.finished_at,
                "frames": self.next_seq}

    async def _append(self, frame: str) -> None:
        data = frame.encode("utf-8", "replace")
        self.frames.append(frame)
        self.bytes += len(data)
        limit = int(_cfg("run_buffer_max_bytes", DEFAULT_BUFFER_BYTES))
        while len(self.frames) > 1 and (self.bytes > limit or len(self.frames) > MAX_FRAMES):
            dropped = self.frames.pop(0)
            self.bytes -= len(dropped.encode("utf-8", "replace"))
            self.base_seq += 1
        if frame.startswith("event: done\n"):
            self._note_done(frame)
        async with self._cond:
            self._cond.notify_all()

    def _note_done(self, frame: str) -> None:
        for line in frame.splitlines():
            if line.startswith("data: "):
                try:
                    d = json.loads(line[6:])
                except ValueError:
                    return
                if isinstance(d, dict):
                    self.state = str(d.get("state") or "completed")
                    self.reason = d.get("reason") or d.get("note")
                return

    async def _finish(self) -> None:
        self.done = True
        self.finished_at = time.time()
        async with self._cond:
            self._cond.notify_all()

    async def wait_beyond(self, seq: int, timeout: float) -> None:
        """Wait until a frame at or after `seq` exists or the run is over (or `timeout`)."""
        async def cond():
            async with self._cond:
                await self._cond.wait_for(lambda: self.next_seq > seq or self.done)
        try:
            await asyncio.wait_for(cond(), timeout)
        except asyncio.TimeoutError:
            pass

    def cancel(self) -> bool:
        if self.done:
            return False
        self.cancel_requested = True
        if self.task is not None:
            self.task.cancel()
        return True


_RUNS: dict = {}


def clear() -> None:
    for h in list(_RUNS.values()):
        if h.task is not None and not h.task.done():
            h.task.cancel()
    _RUNS.clear()


def _purge() -> None:
    now = time.time()
    keep = float(_cfg("run_retention_s", DEFAULT_RETENTION_S))
    for rid, h in list(_RUNS.items()):
        if h.done and (h.acked or (h.finished_at and now - h.finished_at > keep)):
            del _RUNS[rid]


def active_count(user_id: int) -> int:
    return sum(1 for h in _RUNS.values() if h.user_id == user_id and not h.done)


def get(run_id: str) -> Optional[RunHandle]:
    return _RUNS.get(run_id)


def owned(run_id: str, user_id: int) -> Optional[RunHandle]:
    h = _RUNS.get(run_id)
    return h if h is not None and h.user_id == user_id else None


def list_for(user_id: int, session_id: Optional[int] = None) -> list:
    _purge()
    rows = [h for h in _RUNS.values() if h.user_id == user_id and (session_id is None or h.session_id == session_id)]
    return [h.summary() for h in sorted(rows, key=lambda h: h.started)]


def _frame_text(frame) -> str:
    return frame.decode("utf-8", "replace") if isinstance(frame, (bytes, bytearray)) else str(frame)


async def _pump(handle: RunHandle, agen) -> None:
    try:
        async for raw in agen:
            frame = _frame_text(raw)
            if not frame.strip() or frame.startswith(":"):
                continue                        # keep-alive pings: every subscriber makes its own
            await handle._append(frame)
    except asyncio.CancelledError:
        handle.cancel_requested = True
    except Exception as e:                      # the loop already reports its own failures; this is the last resort
        handle.state = "failed"
        await handle._append("event: done\ndata: " + json.dumps(
            {"state": "failed", "reason": f"run_crashed: {type(e).__name__}"}) + "\n\n")
    finally:
        if handle.cancel_requested and handle.state == "running":
            # the loop cannot speak once it is cancelled: tell subscribers how it ended
            await handle._append("event: done\ndata: " + json.dumps(
                {"state": "stopped", "reason": "cancelled", "note": "you stopped the run"}) + "\n\n")
        if handle.state == "running":
            handle.state = "completed" if not handle.cancel_requested else "stopped"
        try:
            await agen.aclose()
        except Exception:
            pass
        await handle._finish()


def start(user_id: int, session_id: Optional[int], agen, run_id: Optional[str] = None) -> RunHandle:
    """Run `agen` (an async generator of SSE frames) as a detached task and return its handle."""
    _purge()
    limit = int(_cfg("max_active_runs_per_user", DEFAULT_MAX_ACTIVE_PER_USER))
    if limit > 0 and active_count(user_id) >= limit:
        raise TooManyRuns(f"you already have {limit} runs in progress; wait for one to finish or stop one")
    rid = run_id if valid_id(run_id) and run_id not in _RUNS else uuid.uuid4().hex
    handle = RunHandle(rid, user_id, session_id)
    _RUNS[rid] = handle
    # created here, inside the request, so the task inherits the request's context (user, device, plan session)
    handle.task = asyncio.get_running_loop().create_task(_pump(handle, agen))
    return handle


async def subscribe(handle: RunHandle, after: int = -1) -> AsyncIterator[str]:
    """Frames of `handle` as SSE, each tagged `id: <seq>`, from just after `after`, following the run live until
    it ends. Cancelling this generator (the client left) leaves the run alone."""
    seq = max(int(after) + 1, 0)
    if seq < handle.base_seq:
        yield (f"event: gap\ndata: {json.dumps({'from': seq, 'resumed_at': handle.base_seq})}\n\n")
        seq = handle.base_seq
    while True:
        while seq < handle.next_seq:
            idx = seq - handle.base_seq
            if idx < 0:                          # the buffer moved past us while we were slow
                seq = handle.base_seq
                continue
            yield f"id: {seq}\n{handle.frames[idx]}"
            seq += 1
        if handle.done:
            return
        before = seq
        await handle.wait_beyond(seq, PING_S)
        if seq == before and handle.next_seq <= seq and not handle.done:
            yield ": ping\n\n"
