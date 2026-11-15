"""Per-user, per-session bookkeeping for the file tools (in memory, bounded).

* read-set   - files the agent has read or created this session; edit_file refuses others
* undo stack - the content before each write, newest last, so any edit can be undone
* verify     - consecutive syntax failures per file and the last content that passed
"""
from collections import OrderedDict
from typing import Optional

from ..request_context import get_current_user_id
from .plans import get_plan_context

MAX_UNDO_PER_FILE = 20
MAX_UNDO_BYTES = 8 * 1024 * 1024      # per user, across files; the oldest entries go first
MAX_TRACKED = 400                      # read-set / verify entries per user

_read_sets: dict = {}                  # scope -> OrderedDict[path, True]
_undo: dict = {}                       # scope -> OrderedDict[path, list[Optional[str]]]
_verify: dict = {}                     # scope -> dict[path, {"fails": int, "good": Optional[str]}]
_written: dict = {}                    # scope -> OrderedDict[path, True]: files this agent created or changed
_aliases: dict = {}                    # scope -> OrderedDict[requested path, resolved path]
_created: dict = {}                    # scope -> OrderedDict[path, True]: files this agent created (did not exist before)


def _scope() -> tuple:
    return (get_current_user_id(), get_plan_context() or 0)


def reset_session(uid=None, session=None) -> None:
    s = (uid if uid is not None else get_current_user_id(), session if session is not None else (get_plan_context() or 0))
    for d in (_read_sets, _undo, _verify, _written, _aliases, _created):
        d.pop(s, None)


# ---- written files (name recovery) ----
def _remember(store: dict, path: str) -> None:
    ws = store.setdefault(_scope(), OrderedDict())
    ws[path] = True
    ws.move_to_end(path)
    while len(ws) > MAX_TRACKED:
        ws.popitem(last=False)


def mark_written(path: str, created: bool = False) -> None:
    """The agent wrote this file; `created` when it did not exist before (only those may be overwritten later)."""
    _remember(_written, path)
    if created:
        _remember(_created, path)


def created_paths() -> set:
    """Files this agent created this session, lower-cased (the file system is case-insensitive on Windows)."""
    return {p.lower() for p in _created.get(_scope(), {})}


def written_paths() -> list:
    """Files this agent wrote this session, oldest first."""
    return list(_written.get(_scope(), {}))


# ---- aliases: a name the agent used -> the file it was resolved to (so the live diff finds the real file) ----
def record_alias(requested: str, actual: str) -> None:
    a = _aliases.setdefault(_scope(), OrderedDict())
    a[requested] = actual
    a.move_to_end(requested)
    while len(a) > MAX_TRACKED:
        a.popitem(last=False)


def alias_target(requested: str) -> Optional[str]:
    return _aliases.get(_scope(), {}).get(requested)


# ---- read-set ----
def mark_read(path: str) -> None:
    rs = _read_sets.setdefault(_scope(), OrderedDict())
    rs[path] = True
    rs.move_to_end(path)
    while len(rs) > MAX_TRACKED:
        rs.popitem(last=False)


def was_read(path: str) -> bool:
    return path in _read_sets.get(_scope(), {})


def restrict_reads(present: set) -> None:
    """Keep only the read-set entries whose file text is still in the model's prompt (`present`: lower-cased absolute
    paths). Clearing, compaction and the executor's fresh context all drop file text; a file whose text is gone is no
    longer 'read', so edit_file asks for a real read_file instead of letting the model edit from memory."""
    rs = _read_sets.get(_scope())
    if not rs:
        return
    for p in [p for p in rs if p.lower() not in present]:
        rs.pop(p, None)


def unmark_read(path: str) -> None:
    """Forget that the file was read: the next edit_file on it is refused until read_file runs again."""
    _read_sets.get(_scope(), {}).pop(path, None)


def failing_files() -> list:
    """Files written this session whose last syntax check failed (the plan's 'done' gate asks)."""
    return sorted(p for p, st in _verify.get(_scope(), {}).items() if st.get("fails", 0) > 0)


# ---- undo ----
def _bytes(stack_map) -> int:
    return sum(len(c or "") for st in stack_map.values() for c in st)


def push_undo(path: str, before: Optional[str]) -> None:
    m = _undo.setdefault(_scope(), OrderedDict())
    st = m.setdefault(path, [])
    st.append(before)
    m.move_to_end(path)
    del st[:-MAX_UNDO_PER_FILE]
    while _bytes(m) > MAX_UNDO_BYTES and m:
        oldest = next(iter(m))
        if len(m[oldest]) > 1:
            m[oldest].pop(0)
        else:
            m.popitem(last=False)


def pop_undo(path: str, steps: int = 1) -> tuple[bool, Optional[str], int]:
    """(found, content_to_restore, steps_undone). content None means the file did not exist."""
    m = _undo.get(_scope(), {})
    st = m.get(path) or []
    if not st:
        return False, None, 0
    n = max(1, min(int(steps or 1), len(st)))
    target = st[-n]
    del st[-n:]
    if not st:
        m.pop(path, None)
    return True, target, n


def undo_depth(path: str) -> int:
    return len(_undo.get(_scope(), {}).get(path) or [])


# ---- verify ----
def verify_state(path: str) -> dict:
    m = _verify.setdefault(_scope(), {})
    if len(m) > MAX_TRACKED:
        m.pop(next(iter(m)))
    return m.setdefault(path, {"fails": 0, "good": None})


def clear_verify(path: str) -> None:
    _verify.get(_scope(), {}).pop(path, None)
