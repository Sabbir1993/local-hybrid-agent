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


# Tool allowlist of the active custom agent for this request (None = no limit).
# Checked by core.agent_loop.run_tool, the one choke point every lane's tool call
# passes through -- filtering the schemas sent to the model is not enough, since a
# model can still emit (or the text parser can recover) a call to any tool name.
_tool_allowlist: contextvars.ContextVar[Optional[frozenset]] = contextvars.ContextVar(
    "tool_allowlist", default=None)


def set_tool_allowlist(names) -> contextvars.Token:
    return _tool_allowlist.set(frozenset(names) if names else None)


def reset_tool_allowlist(token: contextvars.Token) -> None:
    _tool_allowlist.reset(token)


def get_tool_allowlist() -> Optional[frozenset]:
    return _tool_allowlist.get()


def tool_allowed(name: str) -> bool:
    allow = _tool_allowlist.get()
    return allow is None or name in allow


# A Personal Agent run (started from Chat, not Agent Task): the agent works in ONE folder on the user's machine,
# set on the agent itself. It can read, write and analyse there; code execution and sub-agents stay off and
# shell commands must be read-only (core.shell_tools.personal_write_violation). Enforced in
# core.agent_loop.run_tool; the folder replaces the project folder in core.agent_tools.workspace.
_personal_scope: contextvars.ContextVar[bool] = contextvars.ContextVar("personal_scope", default=False)
_personal_workspace: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("personal_workspace", default=None)

PERSONAL_BLOCKED_TOOLS = frozenset({"spawn_agent"})


def set_personal_scope(on: bool) -> contextvars.Token:
    return _personal_scope.set(bool(on))


def personal_scope() -> bool:
    return _personal_scope.get()


def set_personal_workspace(path: Optional[str]) -> contextvars.Token:
    return _personal_workspace.set(path or None)


def personal_workspace() -> Optional[str]:
    return _personal_workspace.get()


# The user approved the device/browser action this tool call is about to make in the web app's permission card.
# core.companion_bridge.call then tells the companion (`approved_in_app`), which skips its own native dialog
# (unless "also confirm on this device" is on). Set by routes/agent/run.py around exactly one tool call.
_device_approved: contextvars.ContextVar[bool] = contextvars.ContextVar("device_approved", default=False)


def set_device_approved(on: bool) -> contextvars.Token:
    return _device_approved.set(bool(on))


def device_approved() -> bool:
    return _device_approved.get()
