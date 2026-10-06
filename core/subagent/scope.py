import contextlib


def drop_child_write_tracking(files_modified: set) -> None:
    """R10: remove THIS child's file writes from the parent's _ws_changes
    (diff/undo set) now that the child's run is over.

    Only paths the child actually wrote (resolved to the server workspace key
    format _ws_changes uses) are dropped - a parallel sibling's files are not
    in `files_modified`, so they survive untouched. Best-effort: any failure
    leaves the parent's diff set as-is rather than breaking the run.
    """
    if not files_modified:
        return
    try:
        from ..agent_tools import _ws_changes, _ws_resolve
        from ..request_context import get_current_user_id
        uid = get_current_user_id()
        if uid is None:
            return
        changes = _ws_changes.get(uid)
        if not isinstance(changes, dict):
            return
        resolved = set()
        for _fp in files_modified:
            try:
                resolved.add(str(_ws_resolve(_fp)))
            except Exception:
                pass
        for _k in list(changes):
            if _k in resolved:
                changes.pop(_k, None)
        if not changes:
            _ws_changes.pop(uid, None)
    except Exception as _e:
        import sys
        print(f"[subagent] ws_changes cleanup failed: {_e}", file=sys.stderr)


@contextlib.asynccontextmanager
async def _subagent_scope():
    """Defensive guard: if a sub-agent's tool run ever mutates the active project
    global, restore it on exit so the parent's workspace pointer survives.
    _ws_changes isolation (R10) is done by the runner, which knows exactly which
    files THIS child wrote - a scope-exit snapshot would drop a parallel
    sibling's write that was not this child's."""
    from ..agent_tools import get_active_project, set_active_project
    before = get_active_project()
    try:
        yield
    finally:
        if get_active_project() != before:
            set_active_project(before)
