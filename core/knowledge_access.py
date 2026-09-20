"""
core/knowledge_access.py - role-based visibility resolution for knowledge sources.

Permission-scoped retrieval, not a post-hoc filter: callers pass the returned
id set into search_memory_hybrid() *before* scoring, so an unpermitted user's
query never even sees a permitted document's chunks as retrieval candidates --
that's what makes "nothing found" safe instead of a leak-prone special case.
"""

from typing import Optional

from . import auth_db


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
