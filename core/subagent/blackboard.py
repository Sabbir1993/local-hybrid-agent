"""core/subagent/blackboard.py - Shared session state for subagent collaboration.

Allows independent or parallel subagents to record verified facts (schemas,
reproduction steps, file locations, test results) into a shared session chalkboard.
Sibling subagents and the parent agent can query this blackboard to avoid redundant
file reads or duplicated investigation.

Constraints:
  * Thread-safe & context-safe.
  * Size-capped per key and per session to prevent memory leaks or context bloat.
  * Secret/PAN masked before persistence.
  * Scoped to session_id (falls back to a default global scope if outside a session).
"""

import contextvars
import json
import threading
import time
from typing import Any, Dict, List, Optional

from ..pan import mask_pans

# Caps to keep blackboard entries lean and prompt-friendly
MAX_ENTRIES_PER_SESSION = 50
MAX_KEY_CHARS = 80
MAX_VAL_CHARS = 2000

_lock = threading.Lock()
_store: Dict[str, Dict[str, dict]] = {}

# Tracking writes within the current execution scope
_active_scope_writes: contextvars.ContextVar[Optional[List[str]]] = contextvars.ContextVar(
    "subagent_scope_writes", default=None
)


def _current_session_id() -> str:
    """Best-effort session identifier from request context, else fallback."""
    try:
        from ..request_context import get_request_context
        ctx = get_request_context()
        if ctx and hasattr(ctx, "session_id") and ctx.session_id:
            return str(ctx.session_id)
    except Exception:
        pass
    return "default_session"


def blackboard_write(key: str, value: str, author: str = "", session_id: Optional[str] = None) -> dict:
    """Record a verified fact into the shared session chalkboard."""
    sid = session_id or _current_session_id()
    clean_key = str(key or "").strip()[:MAX_KEY_CHARS]
    if not clean_key:
        raise ValueError("blackboard key cannot be empty")

    clean_val = str(value or "").strip()[:MAX_VAL_CHARS]
    # PAN / credit card masking so card numbers never linger on the chalkboard
    clean_val, _ = mask_pans(clean_val)

    now = time.time()
    entry = {
        "key": clean_key,
        "value": clean_val,
        "author": str(author or "subagent").strip()[:40],
        "ts": now,
    }

    with _lock:
        if sid not in _store:
            _store[sid] = {}
        sess_dict = _store[sid]
        # Evict oldest entry if cap is reached and key is new
        if clean_key not in sess_dict and len(sess_dict) >= MAX_ENTRIES_PER_SESSION:
            oldest_k = min(sess_dict.keys(), key=lambda k: sess_dict[k].get("ts", 0))
            sess_dict.pop(oldest_k, None)
        sess_dict[clean_key] = entry

    # Track in current active subagent scope if running
    curr_writes = _active_scope_writes.get()
    if curr_writes is not None and clean_key not in curr_writes:
        curr_writes.append(clean_key)

    return entry


def blackboard_read(key: Optional[str] = None, session_id: Optional[str] = None) -> Any:
    """Read a specific key or all entries from the blackboard."""
    sid = session_id or _current_session_id()
    with _lock:
        sess_dict = _store.get(sid, {})
        if key:
            k = str(key).strip()
            return sess_dict.get(k)
        return dict(sess_dict)


def blackboard_clear(session_id: Optional[str] = None) -> None:
    """Clear blackboard entries for a session."""
    sid = session_id or _current_session_id()
    with _lock:
        _store.pop(sid, None)


def blackboard_summary(session_id: Optional[str] = None, max_chars: int = 1500) -> str:
    """Render a concise markdown summary of blackboard entries for model prompts."""
    sid = session_id or _current_session_id()
    with _lock:
        sess_dict = _store.get(sid, {})
        if not sess_dict:
            return ""
        entries = sorted(sess_dict.values(), key=lambda e: e.get("ts", 0))

    lines = ["--- SHARED BLACKBOARD (verified subagent findings) ---"]
    total = len(lines[0])
    for e in entries:
        line = f"• [{e['key']}]: {e['value']}"
        if len(line) > 250:
            line = line[:247] + "..."
        if total + len(line) + 1 > max_chars:
            lines.append("... (more entries on blackboard)")
            break
        lines.append(line)
        total += len(line) + 1
    lines.append("--- END BLACKBOARD ---")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool implementations for the agent tool surface
# ---------------------------------------------------------------------------

async def tool_blackboard_post(args: dict) -> str:
    """Tool: save a verified finding to the shared chalkboard."""
    key = str(args.get("key") or "").strip()
    val = str(args.get("value") or "").strip()
    if not key:
        return "error: 'key' is required (short topic, e.g. 'auth_db_schema')"
    if not val:
        return "error: 'value' is required"
    try:
        entry = blackboard_write(key, val)
        return f"saved to blackboard: [{entry['key']}]"
    except Exception as e:
        return f"error: {e}"


async def tool_blackboard_read(args: dict) -> str:
    """Tool: query findings posted to the shared chalkboard."""
    key = str(args.get("key") or "").strip()
    data = blackboard_read(key or None)
    if not data:
        if key:
            return f"blackboard: no entry found for key '{key}'"
        return "blackboard: chalkboard is currently empty"
    if key and isinstance(data, dict) and "value" in data:
        return f"[{data['key']}]: {data['value']}"
    # Listing all
    out = ["=== Shared Blackboard Entries ==="]
    for k, v in data.items():
        out.append(f"• [{k}]: {v.get('value', '')}")
    return "\n".join(out)
