"""One SYCL llama-server start at a time.

The Intel SYCL / Level Zero runtime segfaults (llama-server exit -11, a general protection fault in libc) when
several llama-server processes initialise the cards at the same moment - the main model, the executor and the
embedder all auto-load together. A start holds this gate from spawn until the server answers /health (or dies), so
the next one begins only after the previous one has finished initialising. Other backends are not gated.
"""
import asyncio
import contextlib

_lock: "asyncio.Lock | None" = None


@contextlib.asynccontextmanager
async def launch_gate(backend):
    global _lock
    if str(backend or "").lower() != "sycl":
        yield
        return
    if _lock is None:
        _lock = asyncio.Lock()
    async with _lock:
        yield
