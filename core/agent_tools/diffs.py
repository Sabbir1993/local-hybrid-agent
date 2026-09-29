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
    rec = _ws_changes.setdefault(uid, {}).setdefault(str(p), {})
    if "before" not in rec:
        rec["before"] = before
    rec["after"] = after
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


async def tool_revert(args: dict) -> str:
    target = args.get("path", "")
    uid_fn = getattr(_pkg(), "_remote_uid", _remote_uid)
    uid = uid_fn()
    cb = getattr(_pkg(), "companion_bridge", companion_bridge)
    changes = _ws_changes.get(uid) or {}
    for path, rec in changes.items():
        if Path(path).name == target or path.endswith(target):
            before = rec.get("before")
            if before is None:
                return (f"error: {target} was created this session; the companion cannot delete "
                        "files -- ask the user to remove it")
            await cb.call(uid, "fs.write", {"path": path, "content": before, "append": False})
            del changes[path]
            return f"reverted {target}"
    return f"error: no tracked change for {target}"
