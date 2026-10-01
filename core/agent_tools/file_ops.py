import fnmatch
import sys
import uuid
from pathlib import Path
from typing import Optional

from .. import companion_bridge
from . import edit_engine
from ..small_model import APP_CONFIG
from . import file_state
from .diffs import _record_diff
from .limits import agent_limit, est_tokens
from .verify_loop import verify_after
from .workspace import (
    DOCUMENT_EXTS,
    MAX_EDIT_BYTES,
    MAX_TOOL_OUTPUT,
    _remote_uid,
    _ws_resolve,
    active_workspace,
    require_device_workspace,
)

# every tool that changes a file's text; shared with the loop, the sandbox check and the UI
FILE_WRITE_TOOLS = ("write_file", "edit_file", "append_file", "insert_at_line")


def _pkg():
    return sys.modules.get("core.agent_tools")


def _cb():
    return getattr(_pkg(), "companion_bridge", companion_bridge)


def _path_arg(args: dict) -> str:
    p = args.get("path") or args.get("file") or args.get("filename")
    if not p:
        raise ValueError("path required")
    return p


def _resolve(path_arg: str):
    """(uid, absolute path on the user's device). Raises when no device workspace is selected."""
    p = getattr(_pkg(), "_ws_resolve", _ws_resolve)(path_arg)
    uid = getattr(_pkg(), "_remote_uid", _remote_uid)()
    return uid, p


async def _read_existing(uid: int, p: Path) -> Optional[str]:
    """The file's text on the device, or None when it does not exist."""
    data = await _cb().call(uid, "fs.read", {"path": str(p)})
    c = data.get("content")
    return c if isinstance(c, str) else None


async def _store(uid: int, p: Path, before: Optional[str], after: str) -> None:
    """Write `after` (whole file), remember `before` for undo and the diff view."""
    await _cb().call(uid, "fs.write", {"path": str(p), "content": after, "append": False})
    file_state.push_undo(str(p), before)
    file_state.mark_read(str(p))
    _record_diff(p, before, after)


def _lines(text: str) -> int:
    return text.count("\n") + (0 if not text or text.endswith("\n") else 1)


def _too_big(content: str, tool: str) -> Optional[str]:
    cap = agent_limit("write_file_max_tokens")
    n = est_tokens(content)
    if n > cap:
        return (f"error: this {tool} content is about {n} tokens; one call may carry at most {cap}. "
                "Write a short skeleton first (imports, class/function signatures, TODO markers) with "
                "write_file, then add each section with append_file or replace a TODO with edit_file - "
                "one section per call, about "
                f"{agent_limit('chunk_target_tokens')} tokens each.")
    return None


async def tool_list_files(args: dict) -> str:
    ws = getattr(_pkg(), "active_workspace", active_workspace)()
    sub = str(args.get("path") or "").strip().strip("/\\")
    root = getattr(_pkg(), "_ws_resolve", _ws_resolve)(sub) if sub else ws
    pat = (args.get("pattern") or "").strip() or "**/*"
    uid = getattr(_pkg(), "_remote_uid", _remote_uid)()
    data = await _cb().call(uid, "fs.list", {"root": str(root), "pattern": pat})
    files = data.get("files") or []
    if sub:
        files = [f"{sub.replace(chr(92), '/')}/{f}" for f in files]
    if not files:
        return "(no files matched)"
    out = "\n".join(files)
    if len(files) >= 200:
        out += "\n(first 200 shown - narrow with path or pattern)"
    return out


async def read_raw(path_arg: str) -> Optional[str]:
    """The file's exact text on the user's device (no line numbers, not marked as read), or None when it
    does not exist. For server code that needs the content itself, e.g. AGENTS.md for the system prompt."""
    uid, p = _resolve(path_arg)
    return await _read_existing(uid, p)


async def tool_read_file(args: dict) -> str:
    path_arg = _path_arg(args)
    uid, p = _resolve(path_arg)
    if p.suffix.lower() in DOCUMENT_EXTS:
        return (f"error: {path_arg} is a binary document - use doc_inspect (outline) or "
                f"read_file_chunk('{path_arg}') instead of read_file.")
    text = await _read_existing(uid, p)
    if text is None:
        return f"error: File not found: '{path_arg}'. It does not exist yet. Use 'write_file' to create it."
    file_state.mark_read(str(p))
    if not text:
        return f"({path_arg} is empty)"
    limit = args.get("limit")
    try:
        limit = int(limit) if limit not in (None, "") else agent_limit("read_file_default_limit")
    except (TypeError, ValueError):
        limit = agent_limit("read_file_default_limit")
    try:
        offset = int(args.get("offset") or args.get("start_line") or 1)
    except (TypeError, ValueError):
        offset = 1
    view = edit_engine.numbered(text, offset, limit, agent_limit("read_file_max_chars"))
    if view["first"] is None:
        return f"error: offset {offset} is past the end - {path_arg} has {view['total']} lines."
    foot = f"[{path_arg}: lines {view['first']}-{view['last']} of {view['total']}"
    if view["next_offset"]:
        foot += f"; {view['total'] - view['last']} more - call read_file with offset={view['next_offset']}, or grep for a name"
    foot += "]"
    return f"{view['text']}\n{foot}"


async def tool_grep(args: dict) -> str:
    pat = (args.get("pattern") or args.get("query") or "").strip()
    if not pat:
        raise ValueError("pattern required")
    import re as _re
    _re.compile(pat, _re.IGNORECASE)
    ws = getattr(_pkg(), "active_workspace", active_workspace)()
    sub = str(args.get("path") or "").strip().strip("/\\")
    root = getattr(_pkg(), "_ws_resolve", _ws_resolve)(sub) if sub else ws
    uid = getattr(_pkg(), "_remote_uid", _remote_uid)()
    data = await _cb().call(uid, "fs.grep", {"root": str(root), "pattern": pat})
    hits = data.get("hits") or []
    glob = str(args.get("glob") or "").strip()
    if glob:
        def _match(h: str) -> bool:
            rel = h.split(":", 1)[0]
            return fnmatch.fnmatch(rel, glob) or fnmatch.fnmatch(rel.rsplit("/", 1)[-1], glob)
        hits = [h for h in hits if _match(h)]
    if sub:
        prefix = sub.replace("\\", "/") + "/"
        hits = [prefix + h for h in hits]
    if not hits:
        return "(no matches)"
    out = "\n".join(hits)
    if len(data.get("hits") or []) >= 100:
        out += "\n(100 matches shown - narrow with path or glob)"
    return out


async def tool_write_file(args: dict) -> str:
    path_arg = args.get("path") or args.get("file") or args.get("filename")
    if not path_arg and args.get("content"):
        c_low = args["content"][:300].lower()
        if "<!doctype html" in c_low or "<html" in c_low:
            path_arg = "index.html"
        elif "def " in c_low or "import " in c_low:
            path_arg = "main.py"
    if not path_arg:
        raise ValueError("path required")
    uid, p = _resolve(path_arg)
    content = args.get("content", "")
    if len(content) > MAX_EDIT_BYTES:
        raise ValueError("content too large")
    append = bool(args.get("append"))

    if p.suffix.lower() in DOCUMENT_EXTS:
        if append:
            raise ValueError(
                f"cannot append to a {p.suffix.lower()} document - send the complete file in one "
                "write_file call, or change an existing document with doc_edit")
        from ..doc_tools import tool_doc_create
        return await tool_doc_create({"file": path_arg, "content": content})
    if append:
        return await tool_append_file({"path": path_arg, "content": content, "_create": True})
    big = _too_big(content, "write_file")
    if big:
        return big

    before = await _read_existing(uid, p)
    if before and not args.get("overwrite"):
        return (f"error: {path_arg} already exists ({_lines(before)} lines). Change part of it with edit_file "
                "(after read_file), add to the end with append_file, or pass overwrite=true to replace the "
                "whole file.")
    await _store(uid, p, before, content)
    verb = "overwrote" if before is not None else "created"
    msg = f"wrote {len(content)} chars ({_lines(content)} lines) to {path_arg} ({verb})"
    return msg + await verify_after(uid, p, path_arg, content, before, can_autorevert=False)


async def tool_append_file(args: dict) -> str:
    path_arg = _path_arg(args)
    uid, p = _resolve(path_arg)
    content = args.get("content", args.get("text", ""))
    if not isinstance(content, str) or not content:
        return "error: content required for append_file"
    if p.suffix.lower() in DOCUMENT_EXTS:
        return (f"error: cannot append to a {p.suffix.lower()} document - use doc_edit to change it.")
    big = _too_big(content, "append_file")
    if big:
        return big
    before = await _read_existing(uid, p)
    if before is None:
        if not args.get("_create"):
            return (f"error: {path_arg} does not exist yet. Create it with write_file (a skeleton or the "
                    "first section), then use append_file for the rest.")
        before = None
    after = edit_engine.append_text(before, content)
    if len(after) > MAX_EDIT_BYTES * 4:
        return f"error: {path_arg} would grow past {MAX_EDIT_BYTES * 4 // 1024} KB - split it into several files."
    await _store(uid, p, before, after)
    msg = (f"appended {len(content)} chars to {path_arg} (now {_lines(after)} lines, "
           f"{len(after)} bytes)")
    return msg + await verify_after(uid, p, path_arg, after, before, can_autorevert=False)


async def tool_edit_file(args: dict) -> str:
    path_arg = _path_arg(args)
    uid, p = _resolve(path_arg)
    old, new = args.get("old_string", ""), args.get("new_string", "")
    if not old:
        raise ValueError("old_string required")
    replace_all = bool(args.get("replace_all"))
    if not file_state.was_read(str(p)):
        return (f"error: read {path_arg} before editing it - call read_file (with offset/limit for the part "
                "you will change) so old_string is copied from the real text.")
    before = await _read_existing(uid, p)
    if before is None:
        raise FileNotFoundError(f"File not found: {path_arg}")
    try:
        res = edit_engine.apply_edit(before, old, new, replace_all)
    except edit_engine.EditError as e:
        return f"error: {e}"
    after = res["text"]
    await _store(uid, p, before, after)
    where = f"line {res['first_line']}" if res["first_line"] == res["last_line"] else \
        f"lines {res['first_line']}-{res['last_line']}"
    note = " (matched ignoring whitespace differences; indentation adjusted)" if res["method"] == "whitespace" else ""
    msg = f"edited {path_arg} at {where}: {res['count']} replacement(s){note}\n{res['snippet']}"
    return msg + await verify_after(uid, p, path_arg, after, before, can_autorevert=True)


async def tool_insert_at_line(args: dict) -> str:
    path_arg = _path_arg(args)
    uid, p = _resolve(path_arg)
    text = args.get("text", args.get("content", ""))
    if not isinstance(text, str) or not text:
        return "error: text required for insert_at_line"
    if not file_state.was_read(str(p)):
        return f"error: read {path_arg} before inserting into it - line numbers must come from read_file."
    big = _too_big(text, "insert_at_line")
    if big:
        return big
    before = await _read_existing(uid, p)
    if before is None:
        raise FileNotFoundError(f"File not found: {path_arg}")
    try:
        res = edit_engine.insert_at_line(before, args.get("line"), text)
    except edit_engine.EditError as e:
        return f"error: {e}"
    await _store(uid, p, before, res["text"])
    msg = f"inserted {res['last_line'] - res['first_line'] + 1} line(s) at line {res['first_line']} of {path_arg}\n{res['snippet']}"
    return msg + await verify_after(uid, p, path_arg, res["text"], before, can_autorevert=True)


async def tool_run_python(args: dict) -> str:
    code = args.get("code", "")
    if not code.strip():
        raise ValueError("code required")
    req_fn = getattr(_pkg(), "require_device_workspace", require_device_workspace)
    uid, ws = req_fn()
    cb = getattr(_pkg(), "companion_bridge", companion_bridge)
    # one script per call: concurrent runs sharing a workspace must never share
    # a filename. The old fixed `_agent_run.py` let a second run overwrite the
    # first run's code between write and execute -- and raced the companion's
    # on-disk approval check (script bytes == approved code) the same way.
    script_name = f"_agent_run_{uuid.uuid4().hex[:8]}.py"
    script = ws / script_name
    await cb.call(uid, "fs.write", {"path": str(script), "content": code, "append": False})
    raw_t = APP_CONFIG.get("agent", {}).get("exec_timeout_s", 0)
    from ..shell_tools import DEFAULT_EXEC_TIMEOUT_S, take_code_approval
    timeout = int(raw_t) if raw_t and int(raw_t) > 0 else DEFAULT_EXEC_TIMEOUT_S
    try:
        data = await cb.call(
            uid, "shell.run", {"command": f'python "{script_name}"', "cwd": str(ws), "timeout": timeout,
                               "display": code, "approved_in_app": take_code_approval(code)},
            timeout=(timeout or 60) + 10)
    except TimeoutError:
        data = None
    finally:
        try:
            await cb.call(uid, "fs.remove", {"path": str(script)})
        except Exception:
            pass
    if data is None:
        return f"error: timed out after {timeout}s (config agent.exec_timeout_s)"
    out = (data.get("stdout") or "")[-MAX_TOOL_OUTPUT:]
    err = (data.get("stderr") or "")[-4000:]
    result = f"exit code {data.get('exit_code')}"
    if out:
        result += f"\n--- stdout ---\n{out}"
    if err:
        result += f"\n--- stderr ---\n{err}"
    return result
