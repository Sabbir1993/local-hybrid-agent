"""Per-tool-call audit trail for agent runs (PCI-DSS 10.x / Bangladesh Bank auditor visibility).

One audit_log row per tool call: who, which run, which tool, a *summary* of the arguments and
whether it worked. The summary keeps locators (paths, patterns, urls, names) and shell commands,
and reduces anything that is content (file bodies, code, edits) to a length and a short hash, so
the audit table never becomes a second copy of the user's code or data. PANs and secret-shaped
values are masked in everything that is kept.
"""

import hashlib
import re
from typing import Any, Optional

from core import pan
from core.audit import audit_log

ACTION = "agent.tool"

# Keys whose value is content, not a locator: never stored, only measured.
_CONTENT_KEYS = frozenset({
    "content", "text", "body", "new_string", "old_string", "diff", "patch", "code", "script",
    "data", "answer", "prompt", "message", "task", "summary", "html", "markdown",
})
_COMMAND_KEYS = frozenset({"command", "cmd"})
_MAX_LOCATOR = 160
_MAX_COMMAND = 200
_MAX_KEYS = 12

_SECRET_RX = re.compile(
    r"(?i)\b((?:api[_-]?key|token|secret|passw(?:or)?d|authorization|bearer)\s*[=:]\s*)\S+")
_BEARER_RX = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}")


def _clean(value: str, limit: int) -> str:
    """Mask PANs and secret-shaped values, then truncate."""
    text, _ = pan.mask_pans(value)
    # bearer first: "Authorization: Bearer <tok>" would otherwise have the scheme word eaten by
    # the key=value pattern and the token left behind
    text = _BEARER_RX.sub(r"\1 [redacted]", text)
    text = _SECRET_RX.sub(r"\1[redacted]", text)
    return text if len(text) <= limit else text[:limit] + "..."


def _measure(value: str) -> dict:
    return {"len": len(value), "sha256": hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:12]}


def summarize_args(args: Any) -> dict:
    """Audit-safe view of a tool's arguments. Never raises."""
    if not isinstance(args, dict):
        return {}
    out: dict = {}
    for i, (key, value) in enumerate(args.items()):
        if i >= _MAX_KEYS:
            out["_more_keys"] = len(args) - _MAX_KEYS
            break
        k = str(key)
        if isinstance(value, str):
            if k in _CONTENT_KEYS:
                out[k] = _measure(value)
            elif k in _COMMAND_KEYS:
                out[k] = _clean(value, _MAX_COMMAND)
            else:
                out[k] = _clean(value, _MAX_LOCATOR)
        elif isinstance(value, (int, float, bool)) or value is None:
            out[k] = value
        elif isinstance(value, (list, tuple, dict)):
            out[k] = {"type": type(value).__name__, "len": len(value)}
        else:
            out[k] = {"type": type(value).__name__}
    return out


def audit_tool_call(user, run_id: str, name: str, args: Any, ok: bool,
                    err_code: Optional[str] = None, step: Optional[int] = None,
                    device_id: Optional[str] = None, session_id: Optional[Any] = None,
                    tainted: bool = False) -> None:
    """Write one audit row. Audit failure must never break a run, so callers need not guard it."""
    try:
        detail = {"run_id": run_id, "step": step, "args": summarize_args(args)}
        if device_id:
            detail["device"] = device_id
        if session_id is not None:
            detail["session_id"] = session_id
        if err_code:
            detail["error"] = str(err_code)[:60]
        if tainted:
            detail["tainted"] = True      # ran after the run had read third-party content
        audit_log(user, action=ACTION, resource=name, result="allow" if ok else "error", detail=detail)
    except Exception:
        pass
