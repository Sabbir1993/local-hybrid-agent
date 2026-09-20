"""
core/request_context.py - per-request "who is asking" context.

A minimal global set once near the top of each request handler (chat/agent/
control/cloud routes all call set_current_user() early), read by code several
layers deep that needs to scope its behavior to the calling user without
threading a user_id parameter through every intervening function signature --
e.g. memory search's per-user session isolation, and per-user cloud provider
config resolution for background helpers (git commit-message generation,
vision describe, reverse-proxy forwarding) that don't have a user_id in their
own signature.

Not a substitute for explicit user_id parameters on primary request-scoped
APIs (routes/cloud.py, routes/control.py pass user.id explicitly) -- this is
the fallback for the deeper, harder-to-thread call sites.
"""

from typing import Optional

_current_user_id: Optional[int] = None


def set_current_user(user_id: Optional[int]) -> None:
    global _current_user_id
    _current_user_id = user_id


def get_current_user_id() -> Optional[int]:
    return _current_user_id
