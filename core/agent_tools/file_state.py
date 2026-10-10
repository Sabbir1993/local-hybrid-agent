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


def _scope() -> tuple:
    return (get_current_user_id(), get_plan_context() or 0)


def reset_session(uid=None, session=None) -> None:
    s = (uid if uid is not None else get_current_user_id(), session if session is not None else (get_plan_context() or 0))
    for d in (_read_sets, _undo, _verify):
        d.pop(s, None)


# ---- read-set ----
def mark_read(path: str) -> None:
    rs = _read_sets.setdefault(_scope(), OrderedDict())
    rs[path] = True
    rs.move_to_end(path)
    while len(rs) > MAX_TRACKED:
        rs.popitem(last=False)


def was_read(path: str) -> bool:
    return path in _read_sets.get(_scope(), {})


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
