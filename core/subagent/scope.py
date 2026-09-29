import contextlib


@contextlib.asynccontextmanager
async def _subagent_scope():
    """Defensive guard: if a sub-agent's tool run ever mutates the active project
    global, restore it on exit so the parent's workspace pointer survives. Does NOT
    isolate _ws_changes."""
    from ..agent_tools import get_active_project, set_active_project
    before = get_active_project()
    try:
        yield
    finally:
        if get_active_project() != before:
            set_active_project(before)
