"""
core/knowledge_access.py - role-based visibility resolution for knowledge sources.

Permission-scoped retrieval, not a post-hoc filter: callers pass the returned
id set into search_memory_hybrid() *before* scoring, so an unpermitted user's
query never even sees a permitted document's chunks as retrieval candidates --
that's what makes "nothing found" safe instead of a leak-prone special case.
"""

import contextvars
from typing import Optional

from . import auth_db

# Data residency: organizational knowledge (HR, salary, internal policy) must
# not be sent to cloud model providers. app.json "knowledge.cloud_policy":
#   "local_only" (default) -- KB context/tool results only ever reach local models
#   "allow"                -- legacy behavior, KB may be sent to cloud lanes
_kb_blocked: contextvars.ContextVar[bool] = contextvars.ContextVar("kb_blocked_for_cloud", default=False)

KB_CLOUD_BLOCKED_MSG = ("error: the company knowledge base is only available to local models, and this "
                        "request uses a cloud model. Switch to a local lane to query internal knowledge.")


def kb_local_only() -> bool:
    from .small_model import APP_CONFIG
    return str((APP_CONFIG.get("knowledge") or {}).get("cloud_policy", "local_only")).lower() != "allow"


def set_kb_cloud_blocked(any_cloud_lane: bool) -> bool:
    """Per request: block KB retrieval when a cloud lane may see the results.
    Returns the resulting blocked flag."""
    blocked = bool(any_cloud_lane) and kb_local_only()
    _kb_blocked.set(blocked)
    return blocked


def kb_cloud_blocked() -> bool:
    return _kb_blocked.get()


def allowed_source_ids_for(principal) -> set:
    """Knowledge source ids this principal may retrieve chunks from.

    Super admin sees every *ready* (indexed) source -- still governed by the
    same query-time filter, not a bypass of the search itself."""
    if principal is None:
        return set()
    ready = {r["id"] for r in auth_db.list_knowledge_sources() if r["status"] == "ready"}
    if principal.is_super_admin:
        return ready
    ids = auth_db.allowed_knowledge_source_ids_for_roles(principal.role_names)
    return ids & ready
