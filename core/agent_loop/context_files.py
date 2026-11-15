"""Which files' text is actually in the prompt the model is about to see.

edit_file refuses a file that was not read, so the model copies old_string from real text. But context management
rewrites the prompt: old tool results become one-line placeholders (clearing.py), whole earlier steps become a digest
(compaction.py) and the executor starts from a fresh view (executor_view.py). A file read or written before that point
is still 'read' in file_state, yet its text is gone, so the model edits from memory and invents the lines it needs.
sync_reads() makes the read-set match the prompt just before each model call."""
from ..agent_tools import file_state
from .clearing import CLEARED_PREFIX
from .compaction import _call_index

# a tool result that carries the text of a file (or the part the model is about to change)
TEXT_RESULTS = frozenset({"read_file", "edit_file", "append_file", "write_file"})
# a tool call whose arguments carry text the model wrote itself
TEXT_CALLS = frozenset({"write_file", "append_file", "edit_file"})


def _path(args) -> str:
    if not isinstance(args, dict):
        return ""
    return str(args.get("path") or args.get("file") or args.get("filename") or "").strip()


def paths_in_view(msgs: list) -> set:
    """Paths (as the model wrote them) whose text appears in `msgs`: an uncleared read/edit result, or a write call."""
    calls = _call_index(msgs)
    out = set()
    for m in msgs:
        role = m.get("role")
        if role == "tool":
            if str(m.get("content") or "").startswith(CLEARED_PREFIX):
                continue                        # a placeholder and the top of the file, not the text
            name, args = calls.get(m.get("tool_call_id")) or ("", {})
            if name in TEXT_RESULTS and _path(args):
                out.add(_path(args))
        elif role == "assistant":
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function") or {}
                if fn.get("name") in TEXT_CALLS:
                    name, args = calls.get(tc.get("id")) or ("", {})
                    if _path(args):
                        out.add(_path(args))
    return out


def sync_reads(msgs: list) -> None:
    """Drop 'read' status from every file whose text is not in `msgs`, the prompt about to be sent."""
    from ..agent_tools.workspace import _ws_resolve
    present = set()
    for p in paths_in_view(msgs):
        try:
            present.add(str(_ws_resolve(p)).lower())
        except Exception:
            continue                            # a path outside the workspace cannot be an edit target anyway
    file_state.restrict_reads(present)
