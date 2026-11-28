"""Routes each request to the main model to one llama-server instance (multi mode).

Single mode: nothing changes; the transport passes every request through. Multi mode: instance 0 is `state.process` on
the primary port, the others are `state.extras`. A request goes to the instance the admission gate chose for it
(`_instance_choice`, core/serving_slots.py place); without a choice, to the healthy instance with the fewest requests
in flight. A chat sticks to one instance because its prompt cache lives there.
"""

import contextvars
import time
from typing import Optional

import httpx

from .config import LLAMA_SERVER_PORT

_instance_choice: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar("main_instance_choice", default=None)

# requests currently sent to each instance through the transport (index -> count)
inflight: dict = {}


def _state():
    from .state import state          # late: core.state wires this module in
    return state


def is_multi() -> bool:
    s = _state()
    return (getattr(s, "launch_info", None) or {}).get("mode") == "multi" and bool(getattr(s, "extras", None))


def _primary_slots(s) -> int:
    info = getattr(s, "launch_info", None) or {}
    for inst in info.get("instances") or []:
        if isinstance(inst, dict) and int(inst.get("index") or 0) == 0:
            return max(1, int(inst.get("n_slots") or 1))
    return max(1, int(info.get("n_slots") or (getattr(s, "profile", None) or {}).get("n_slots") or 1))


def live_instances() -> list[dict]:
    """Every instance with its port, slot count and health. Single mode: the primary only."""
    s = _state()
    p = getattr(s, "process", None)
    primary_ok = bool(p is not None and p.poll() is None and getattr(s, "healthy", False))
    out = [{"index": 0, "port": LLAMA_SERVER_PORT, "n_slots": _primary_slots(s), "healthy": primary_ok}]
    if is_multi():
        for e in s.extras:
            ok = bool(getattr(e, "healthy", False) and e.is_running())
            out.append({"index": int(e.index), "port": int(e.port), "n_slots": int(e.n_slots), "healthy": ok})
    return out


def port_for(index: int) -> Optional[int]:
    for i in live_instances():
        if i["index"] == index:
            return i["port"]
    return None


def least_busy_index() -> Optional[int]:
    """The healthy instance with the fewest requests in flight (ties: lowest index), or None when none is healthy."""
    healthy = [i["index"] for i in live_instances() if i["healthy"]]
    if not healthy:
        return None
    return min(healthy, key=lambda idx: (inflight.get(idx, 0), idx))


def _target() -> Optional[int]:
    healthy = {i["index"] for i in live_instances() if i["healthy"]}
    if not healthy:
        return None
    chosen = _instance_choice.get()
    if chosen in healthy:
        return chosen
    return least_busy_index()


def _release(index: int) -> None:
    inflight[index] = max(0, inflight.get(index, 1) - 1)


class _CountedStream(httpx.AsyncByteStream):
    """Holds the instance's in-flight count until the response is closed (or dropped)."""

    def __init__(self, inner, index: int):
        self._inner = inner
        self._index = index
        self._open = True

    async def __aiter__(self):
        async for chunk in self._inner:
            yield chunk

    async def aclose(self) -> None:
        if self._open:
            self._open = False
            _release(self._index)
        close = getattr(self._inner, "aclose", None)
        if close is not None:
            await close()

    def __del__(self):
        if getattr(self, "_open", False):
            self._open = False
            _release(self._index)


class RouterTransport(httpx.AsyncBaseTransport):
    """Wraps the engine transport (state.client). Single mode: passes requests through untouched."""

    def __init__(self, inner: httpx.AsyncBaseTransport):
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not is_multi():
            return await self._inner.handle_async_request(request)
        target = _target()
        if target is None:
            # nothing healthy: the primary port, so the caller gets the usual connection error
            port = port_for(0)
            if port is not None:
                request.url = request.url.copy_with(port=port)
            return await self._inner.handle_async_request(request)
        request.url = request.url.copy_with(port=port_for(target))
        inflight[target] = inflight.get(target, 0) + 1
        if target:
            # real traffic keeps an extra server warm, so its keepalive ping only runs when it sits idle
            for e in getattr(_state(), "extras", None) or []:
                if int(getattr(e, "index", -1)) == target:
                    e.last_activity = time.time()
        try:
            resp = await self._inner.handle_async_request(request)
        except BaseException:
            _release(target)
            raise
        resp.stream = _CountedStream(resp.stream, target)
        return resp

    async def aclose(self) -> None:
        await self._inner.aclose()


def wrap_transport(inner: httpx.AsyncBaseTransport) -> RouterTransport:
    return RouterTransport(inner)
