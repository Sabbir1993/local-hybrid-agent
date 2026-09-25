"""
core/request_context.py - per-request "who is asking" context.

A per-request value set once near the top of each request handler (chat/agent/
control/cloud routes all call set_current_user() early), read by code several
layers deep that needs to scope its behavior to the calling user without
threading a user_id parameter through every intervening function signature --
e.g. memory search's per-user session isolation, and per-user cloud provider
config resolution for background helpers (git commit-message generation,
vision describe, reverse-proxy forwarding) that don't have a user_id in their
own signature.

Backed by contextvars, so concurrent requests (each running in its own asyncio
task) never see each other's user/device. Code that hands work to a thread pool
must carry the context across with run_in_executor_ctx() below --
loop.run_in_executor() does NOT copy contextvars on its own.

Not a substitute for explicit user_id parameters on primary request-scoped
APIs (routes/cloud.py, routes/control.py pass user.id explicitly) -- this is
the fallback for the deeper, harder-to-thread call sites.
"""

import asyncio
import contextvars
import functools
import hashlib
import re
from typing import Any, Callable, Optional

_current_user_id: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "current_user_id", default=None)
_current_device_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "current_device_id", default=None)
_current_device_name: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "current_device_name", default=None)


def set_current_user(user_id: Optional[int]) -> None:
    _current_user_id.set(user_id)


def get_current_user_id() -> Optional[int]:
    return _current_user_id.get()


# Companions used to report a slice of the OS machine GUID (dev_win_<hex>, dev_lnx_...,
# dev_mac_...): a stable hardware identifier. It is never stored as-is - it's replaced
# by a one-way hash, which the rebuilt companion also sends itself (so old and new
# companions map to the same projects).
_HW_DEVICE_RX = re.compile(r"^dev_(win|lnx|mac)_[0-9a-z]+$")


def device_hash(raw: str) -> str:
    return "dev_h_" + hashlib.sha256(("a770-device:" + raw).encode("utf-8")).hexdigest()[:24]


def normalize_device_id(device_id: Optional[str]) -> Optional[str]:
    if device_id and _HW_DEVICE_RX.match(device_id):
        return device_hash(device_id)
    return device_id


def set_current_device(device_id: Optional[str], device_name: Optional[str] = None) -> None:
    _current_device_id.set(normalize_device_id(device_id))
    if device_name is not None:
        _current_device_name.set(device_name)


def get_current_device_id() -> Optional[str]:
    return _current_device_id.get() or "default"


def get_current_device_name() -> Optional[str]:
    return _current_device_name.get() or "Default Device"


async def run_in_executor_ctx(fn: Callable[..., Any], *args: Any) -> Any:
    """loop.run_in_executor(None, fn, *args), but with the caller's contextvars
    (current user/device) visible inside the worker thread."""
    ctx = contextvars.copy_context()
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(ctx.run, fn, *args))
