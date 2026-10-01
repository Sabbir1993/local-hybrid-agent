"""Model-facing wrappers for the long-term memory store (core/agent_memory.py)."""
from .. import agent_memory
from ..request_context import get_current_user_id

MEMORY_TOOL_NAMES = frozenset({"memory_list", "memory_read", "memory_write", "memory_str_replace",
                               "memory_append", "memory_delete"})


def _uid() -> int:
    uid = get_current_user_id()
    if uid is None:
        raise ValueError("not signed in")
    return uid


def _guarded(fn):
    def run(args: dict) -> str:
        try:
            return fn(_uid(), args or {})
        except agent_memory.MemoryError_ as e:
            return f"error: {e}"
    run.__name__ = fn.__name__
    return run


def _saved(f: dict, verb: str = "saved") -> str:
    return f"{verb} {f['path']} ({f['bytes']} bytes, version {f['version']})"


@_guarded
def tool_memory_list(uid: int, args: dict) -> str:
    files = agent_memory.list_files(uid)
    if not files:
        return "(no memory files yet)"
    return "\n".join(f"{f['path']} - {f['description']} ({f['bytes']} bytes, version {f['version']})"
                     for f in files)


@_guarded
def tool_memory_read(uid: int, args: dict) -> str:
    paths = args.get("paths") or args.get("path")
    if isinstance(paths, str):
        paths = [paths]
    if not isinstance(paths, list) or not paths:
        return "error: path required (or paths: a list of up to 5)"
    out = []
    for p in paths[:5]:
        f = agent_memory.read(uid, str(p))
        out.append(f"[{f['path']} - version {f['version']}]\n{f['text']}" if f
                   else f"[{p}] does not exist")
    return "\n\n".join(out)


@_guarded
def tool_memory_write(uid: int, args: dict) -> str:
    if "content" not in args:
        return "error: content required"
    f = agent_memory.write(uid, args.get("path"), args.get("content"), args.get("if_version"),
                           args.get("description"))
    return _saved(f)


@_guarded
def tool_memory_str_replace(uid: int, args: dict) -> str:
    f = agent_memory.str_replace(uid, args.get("path"), args.get("old_str", ""), args.get("new_str", ""),
                                 args.get("if_version"))
    return _saved(f, "updated")


@_guarded
def tool_memory_append(uid: int, args: dict) -> str:
    f, changed = agent_memory.append(uid, args.get("path"), args.get("line", ""), args.get("if_version"),
                                     args.get("description"))
    return _saved(f, "added to") if changed else f"already stored in {f['path']} (no change)"


@_guarded
def tool_memory_delete(uid: int, args: dict) -> str:
    if not agent_memory.deletion_requested():
        return ("error: memory_delete only runs when the user asks to forget or delete something in their "
                "latest message. To drop a single fact use memory_str_replace with an empty new_str.")
    return (f"deleted {args.get('path')}" if agent_memory.delete(uid, args.get("path"), args.get("if_version"))
            else f"{args.get('path')} does not exist")


def _fn(name, desc, props, required):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props, "required": required}}}


_P = {"type": "string", "description": "memory file, e.g. preferences.md, profile.md, lessons.md, projects/<name>.md"}
_V = {"type": "string", "description": "version token from memory_read/memory_list; the write is refused if the file changed since"}

MEMORY_SCHEMAS = [
    _fn("memory_list", "List your long-term memory files with a one-line description each.", {}, []),
    _fn("memory_read", "Read one or more memory files (returns the text and its version).",
        {"path": _P, "paths": {"type": "array", "items": {"type": "string"}}}, []),
    _fn("memory_write", "Rewrite a whole memory file. The content starts with a three-dash line, a line 'description: <one line>', "
        "another three-dash line, "
        "followed by short '- fact' lines. Read the file first and keep what is still true.",
        {"path": _P, "content": {"type": "string"}, "if_version": _V}, ["path", "content"]),
    _fn("memory_str_replace", "Change or remove one line of a memory file (empty new_str removes it).",
        {"path": _P, "old_str": {"type": "string"}, "new_str": {"type": "string"}, "if_version": _V},
        ["path", "old_str", "new_str"]),
    _fn("memory_append", "Add a fact the user stated to a memory file (skipped if already there). Creates the file "
        "when description is given.",
        {"path": _P, "line": {"type": "string", "description": "one short fact, in the user's own terms"},
         "description": {"type": "string", "description": "only when creating the file: what it covers"},
         "if_version": _V}, ["path", "line"]),
    _fn("memory_delete", "Delete a whole memory file. Only when the user explicitly asks to forget or delete it.",
        {"path": _P, "if_version": _V}, ["path"]),
]

MEMORY_IMPLS = {
    "memory_list": tool_memory_list,
    "memory_read": tool_memory_read,
    "memory_write": tool_memory_write,
    "memory_str_replace": tool_memory_str_replace,
    "memory_append": tool_memory_append,
    "memory_delete": tool_memory_delete,
}
