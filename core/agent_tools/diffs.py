import difflib
import sys
from pathlib import Path
from typing import Optional

from .. import companion_bridge
from ..request_context import get_current_user_id
from .workspace import _remote_uid, _ws_changes, _ws_resolve

_file_diffs: dict = {}       # (uid, path) -> per-call diff summary, popped into the tool_result event
MAX_DIFF_LINES = 400


def _pkg():
    return sys.modules.get("core.agent_tools")


MAX_TRACKED_FILES = 50      # per user: each entry holds a whole before/after text, so a long session must not keep them all


def _trim_changes(changes: dict) -> None:
    """Drop the oldest tracked files beyond MAX_TRACKED_FILES (their 'revert' baseline goes with them)."""
    while len(changes) > MAX_TRACKED_FILES:
        changes.pop(next(iter(changes)))


def _snapshot_change(p: Path) -> None:
    changes = _ws_changes.setdefault(get_current_user_id(), {})
    key = str(p)
    rec = changes.setdefault(key, {})
    if "before" not in rec:
        try:
            rec["before"] = p.read_text(encoding="utf-8", errors="replace") if p.exists() else None
        except Exception:
            rec["before"] = None
    rec["after"] = "written"
    _trim_changes(changes)


async def _remote_read_or_none(uid: int, p: Path) -> Optional[str]:
    """Current content of a file on the companion machine, or None if missing/unreadable."""
    cb = getattr(_pkg(), "companion_bridge", companion_bridge)
    try:
        data = await cb.call(uid, "fs.read", {"path": str(p)})
    except Exception:
        return None
    content = data.get("content")
    return content if isinstance(content, str) else None


def diff_summary(before: Optional[str], after: str) -> dict:
    """{created, added, removed, hunks, truncated} -- unified diff with 3 lines of context."""
    a = (before or "").splitlines()
    b = (after or "").splitlines()
    hunks, added, removed = [], 0, 0
    for ln in list(difflib.unified_diff(a, b, lineterm="", n=3))[2:]:
        if ln.startswith("@@"):
            hunks.append({"t": "@", "s": ln})
        elif ln.startswith("+"):
            added += 1
            hunks.append({"t": "+", "s": ln[1:]})
        elif ln.startswith("-"):
            removed += 1
            hunks.append({"t": "-", "s": ln[1:]})
        else:
            hunks.append({"t": " ", "s": ln[1:]})
    return {"created": before is None, "added": added, "removed": removed,
            "hunks": hunks[:MAX_DIFF_LINES], "truncated": len(hunks) > MAX_DIFF_LINES}


def _record_diff(p: Path, before: Optional[str], after: str) -> None:
    """Track a companion-side write: session baseline for the workspace panel,
    plus this call's own diff for the activity feed."""
    uid = get_current_user_id()
    changes = _ws_changes.setdefault(uid, {})
    rec = changes.setdefault(str(p), {})
    if "before" not in rec:
        rec["before"] = before
    rec["after"] = after
    _trim_changes(changes)
    _file_diffs[(uid, str(p))] = diff_summary(before, after)


def pop_file_diff(args: dict) -> Optional[dict]:
    """The diff recorded by the last write_file/edit_file call on this path (once)."""
    path_arg = (args or {}).get("path") or (args or {}).get("file") or (args or {}).get("filename")
    if not path_arg:
        return None
    res_fn = getattr(_pkg(), "_ws_resolve", _ws_resolve)
    try:
        p = res_fn(path_arg)
    except Exception:
        return None
    return _file_diffs.pop((get_current_user_id(), str(p)), None)


def tool_list_diff(args: dict) -> str:
    changes = _ws_changes.get(get_current_user_id()) or {}
    if not changes:
        return "(no tracked changes in this session)"
    out = []
    for path, rec in changes.items():
        rel = Path(path).name
        status = "created" if rec.get("before") is None else "modified"
        out.append(f"{rel}: {status}")
    return "\n".join(out)


async def _restore(uid: int, path: str, content: Optional[str]) -> Optional[str]:
    """Put `content` back on the device (None = the file did not exist: delete it).
    Returns an error text, or None on success."""
    cb = getattr(_pkg(), "companion_bridge", companion_bridge)
    if content is None:
        try:
            await cb.call(uid, "fs.remove", {"path": path})
        except Exception:
            return ("the file was created this session and this companion version cannot delete files - "
                    "ask the user to remove it (or update the companion)")
        return None
    await cb.call(uid, "fs.write", {"path": path, "content": content, "append": False})
    return None


async def tool_revert(args: dict) -> str:
    """Undo the agent's last edit(s) to one file. steps=N undoes N edits; to_start=true goes back
    to the file as it was before the agent touched it this session."""
    from . import file_state
    target = str(args.get("path", "")).strip()
    if not target:
        return "error: path required"
    uid_fn = getattr(_pkg(), "_remote_uid", _remote_uid)
    uid = uid_fn()
    try:
        full = str(getattr(_pkg(), "_ws_resolve", _ws_resolve)(target))
    except Exception:
        full = ""
    changes = _ws_changes.get(uid) or {}
    key = next((k for k in changes if k == full), None) or next(
        (k for k in changes if Path(k).name == target or k.endswith(target)), None)
    key = key or full
    to_start = bool(args.get("to_start"))
    steps = args.get("steps") or 1

    if not to_start and key and file_state.undo_depth(key):
        found, content, n = file_state.pop_undo(key, steps)
        err = await _restore(uid, key, content)
        if err:
            return f"error: {err}"
        rec = changes.get(key)
        if rec is not None:
            if rec.get("before") == content and not file_state.undo_depth(key):
                del changes[key]
            else:
                rec["after"] = content
        file_state.clear_verify(key)
        left = file_state.undo_depth(key)
        return f"reverted {target} ({n} edit(s) undone; {left} earlier version(s) still available)"

    rec = changes.get(key)
    if rec is None:
        return f"error: no tracked change for {target}"
    err = await _restore(uid, key, rec.get("before"))
    if err:
        return f"error: {err}"
    del changes[key]
    file_state.clear_verify(key)
    return f"reverted {target} to its state before this session"
