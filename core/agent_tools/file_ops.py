import difflib
import fnmatch
import sys
import uuid
from pathlib import Path
from typing import Optional

from .. import companion_bridge
from ..request_context import personal_scope
from . import edit_engine
from ..small_model import APP_CONFIG
from . import file_state
from .diffs import _record_diff
from .limits import agent_limit, effective_chunk_tokens, est_tokens
from .name_recovery import match_written
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
    file_state.mark_written(str(p), created=before is None)   # name recovery + which files write_file may later overwrite
    _record_diff(p, before, after)


def _lines(text: str) -> int:
    return text.count("\n") + (0 if not text or text.endswith("\n") else 1)


SHRINK_MIN_CHARS = 600     # a file this big, built by the agent, is not replaced by a much smaller one by accident
SHRINK_RATIO = 0.5


def _too_big(content: str, tool: str, args: Optional[dict] = None, existing: bool = False) -> Optional[str]:
    cap = agent_limit("write_file_max_tokens")
    # `_oversize_tokens`: the stream was cut once the call passed the cap (routes/common/sse_stream.py), so
    # `content` is empty and the size is the amount generated before the cut
    n = int((args or {}).get("_oversize_tokens") or 0) or est_tokens(content)
    if n > cap and (existing or tool in ("append_file", "edit_file")):
        # the file already holds the earlier sections: "write a skeleton first" here sends the model back to square one,
        # where it rewrites the file and loses them
        return (f"error: this {tool} content is about {n} tokens; one call may carry at most {cap}. The file already "
                "holds everything written so far - do NOT rewrite or restart it. Send only the next section, about "
                f"{effective_chunk_tokens()} tokens (roughly 80 lines), with append_file, then the section after it.")
    if n > cap:
        return (f"error: this {tool} content is about {n} tokens; one call may carry at most {cap}. "
                "Write a short skeleton first (imports, class/function signatures, TODO markers) with "
                "write_file, then add each section with append_file or replace a TODO with edit_file - "
                "one section per call, about "
                f"{effective_chunk_tokens()} tokens each.")
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


async def _near_names(uid: int, p: Path) -> list[str]:
    """Names of files in the same folder that are close to p's name (exact names come back as-is)."""
    try:
        data = await _cb().call(uid, "fs.list", {"root": str(p.parent), "pattern": "*"})
    except Exception:
        return []
    files = [f for f in (data.get("files") or []) if isinstance(f, str)]
    return difflib.get_close_matches(p.name, [Path(f).name for f in files], n=3, cutoff=0.6)


async def _suggest_names(uid: int, p: Path) -> list[str]:
    """Real names the agent most likely meant: the file it wrote under a close name first, then close names on disk."""
    names: list[str] = []
    w = match_written(p, file_state.written_paths())
    if w is not None:
        names.append(w.name)
    for n in await _near_names(uid, p):
        if n not in names:
            names.append(n)
    return names[:3]


async def _missing_msg(uid: int, p: Path, path_arg: str, tail: str) -> str:
    near = await _suggest_names(uid, p)
    if near:   # the file most likely exists under a name the agent mistyped: point at real names, never create
        return (f"error: File not found: '{path_arg}'. Files with a close name in that folder: "
                f"{', '.join(near)}. Copy one name exactly.")
    return f"error: File not found: '{path_arg}'. {tail}"


async def _free_name(uid: int, p: Path) -> str:
    """The first unused 'name_2.ext', 'name_3.ext' ... in p's folder: short enough for a model to copy exactly."""
    try:
        data = await _cb().call(uid, "fs.list", {"root": str(p.parent), "pattern": "*"})
        taken = {Path(f).name.lower() for f in (data.get("files") or []) if isinstance(f, str)}
    except Exception:
        taken = set()
    for n in range(2, 100):
        cand = f"{p.stem}_{n}{p.suffix}"
        if cand.lower() not in taken:
            return cand
    return f"{p.stem}_new{p.suffix}"


async def _personal_protect(uid: int, p: Path, path_arg: str, before: Optional[str]) -> Optional[str]:
    """A Personal Agent never changes a file it did not create this session with write_file: it saves under a new name.
    (Its own files can be rewritten, edit_file changes any file after a read.) Replaces the old random-id rename, which
    a model could not copy back reliably: the agent keeps the name it chose and is told a short free one on a clash."""
    if before is None or not personal_scope() or str(p).lower() in file_state.created_paths():
        return None
    return (f"error: {path_arg} already exists in your folder and was not created in this session, so write_file will "
            f"not change it. Save your work under another name, for example {await _free_name(uid, p)}. "
            "To change that file instead, use edit_file.")


async def _recover_written(uid: int, p: Path, path_arg: str):
    """(path, text, arg) for the file this agent wrote under a close name, when the one asked for is missing.
    Read-only callers only: anything that writes or runs code must use the exact name instead."""
    cand = match_written(p, file_state.written_paths())
    if cand is None:
        return None
    text = await _read_existing(uid, cand)
    if text is None:
        return None
    rel = str(path_arg).replace("\\", "/")
    return cand, text, (rel.rsplit("/", 1)[0] + "/" + cand.name if "/" in rel else cand.name)


async def tool_read_file(args: dict) -> str:
    path_arg = _path_arg(args)
    uid, p = _resolve(path_arg)
    if p.suffix.lower() in DOCUMENT_EXTS:
        return (f"error: {path_arg} is a binary document - use doc_inspect (outline) or "
                f"read_file_chunk('{path_arg}') instead of read_file.")
    text = await _read_existing(uid, p)
    note = ""
    if text is None:
        rec = await _recover_written(uid, p, path_arg)   # read-only, so a unique close match to a file we wrote is safe
        if rec is None:
            return await _missing_msg(uid, p, path_arg, "It does not exist yet. Use 'write_file' to create it.")
        p, text, new_arg = rec
        note = (f"note: '{path_arg}' was not found; this is '{new_arg}', the file you wrote. "
                "Use that exact name from now on.\n")
        path_arg = new_arg
    file_state.mark_read(str(p))
    if not text:
        return note + f"({path_arg} is empty)"
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
        return note + (f"note: offset {offset} is past the end - {path_arg} has only {view['total']} lines, so there is nothing more "
                       f"to read. Use what you already read (lines 1-{view['total']} are all of it).")
    foot = f"[{path_arg}: lines {view['first']}-{view['last']} of {view['total']}"
    if view["next_offset"]:
        foot += f"; {view['total'] - view['last']} more - call read_file with offset={view['next_offset']}, or grep for a name"
    foot += "]"
    return f"{note}{view['text']}\n{foot}"


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
    if args.get("_oversize_tokens"):
        existing = False
        try:                               # a cut-off write over a file that already has content must not say "start over"
            _uid, _p = _resolve(path_arg)
            existing = await _read_existing(_uid, _p) is not None
        except Exception:
            pass
        return _too_big("", "write_file", args, existing=existing)
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
        if personal_scope():
            prot = await _personal_protect(uid, p, path_arg, await _read_existing(uid, p))
            if prot:
                return prot
        return await tool_append_file({"path": path_arg, "content": content, "_create": True})
    before = await _read_existing(uid, p)
    prot = await _personal_protect(uid, p, path_arg, before)
    if prot:
        return prot
    if before and not args.get("overwrite"):
        return (f"error: {path_arg} already exists ({_lines(before)} lines). Change part of it with edit_file "
                "(after read_file), add to the end with append_file, or pass overwrite=true to replace the "
                "whole file.")
    if p.suffix.lower() not in (".html", ".htm", ".csv", ".tsv", ".svg"):
        big = _too_big(content, "write_file", existing=before is not None)
        if big:
            return big
    if (before and not args.get("shrink_ok") and len(before) >= SHRINK_MIN_CHARS
            and len(content) < len(before) * SHRINK_RATIO and str(p).lower() in {w.lower() for w in file_state.written_paths()}):
        # restarting a half-built file with its skeleton throws the finished sections away: make that a deliberate step
        return (f"error: overwrite=true would replace {_lines(before)} lines of {path_arg} with only {_lines(content)} - "
                "that deletes the sections you already added. Do not restart the file: read_file it to see where you are, "
                "then add the next section with append_file (or change part of it with edit_file). Only if you really want "
                "the shorter file, repeat write_file with overwrite=true and shrink_ok=true.")
    if args.get("pre_save_check", True) and not args.get("skip_verify"):
        from .verify import validate_code_syntax
        is_ok, err = validate_code_syntax(path_arg, content)
        if not is_ok:
            return f"error: pre-save syntax validation failed for {path_arg}: {err} (file was not written)"
    await _store(uid, p, before, content)
    verb = "overwrote" if before is not None else "created"
    msg = f"wrote {len(content)} chars ({_lines(content)} lines) to {path_arg} ({verb})"
    return msg + await verify_after(uid, p, path_arg, content, before, can_autorevert=False)


async def _retry_on_written_name(impl, args: dict) -> str:
    """Run a write tool; when its file is missing but is the one this agent wrote under a close name (the server renames
    created files to '<name>_<8 hex>', which a model cannot copy reliably), run it again on the real file and say so.
    Costs nothing when the file is found; never touches a file the agent did not write."""
    out = await impl(args)
    if args.get("_create") or not (isinstance(out, str) and out.startswith("error: File not found:")):
        return out
    path_arg = str(args.get("path") or args.get("file") or args.get("filename") or "")
    if not path_arg:
        return out
    uid, p = _resolve(path_arg)
    rec = await _recover_written(uid, p, path_arg)
    if rec is None:
        return out
    cand, _text, new_arg = rec
    file_state.record_alias(str(p), str(cand))      # so the live diff, keyed by the name in the call, finds the real file
    fixed = dict(args)
    for k in ("path", "file", "filename"):
        if fixed.get(k):
            fixed[k] = new_arg
    note = f"note: '{path_arg}' was not found; used '{new_arg}', the file you wrote. Use that exact name from now on.\n"
    return note + await impl(fixed)


async def tool_append_file(args: dict) -> str:
    return await _retry_on_written_name(_append_file, args)


async def tool_edit_file(args: dict) -> str:
    return await _retry_on_written_name(_edit_file, args)


async def _append_file(args: dict) -> str:
    path_arg = _path_arg(args)
    if args.get("_oversize_tokens"):
        return _too_big("", "append_file", args)
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
            return await _missing_msg(uid, p, path_arg, "Create it with write_file (a skeleton or the "
                                      "first section), then use append_file for the rest.")
        before = None
    after = edit_engine.append_text(before, content)
    if len(after) > MAX_EDIT_BYTES * 4:
        return f"error: {path_arg} would grow past {MAX_EDIT_BYTES * 4 // 1024} KB - split it into several files."
    if args.get("pre_save_check", True) and not args.get("skip_verify"):
        from .verify import validate_code_syntax
        is_ok, err = validate_code_syntax(path_arg, after)
        if not is_ok:
            return f"error: pre-save syntax validation failed for {path_arg}: {err} (file was not written)"
    await _store(uid, p, before, after)
    msg = (f"appended {len(content)} chars to {path_arg} (now {_lines(after)} lines, "
           f"{len(after)} bytes)")
    return msg + await verify_after(uid, p, path_arg, after, before, can_autorevert=False)


async def _edit_file(args: dict) -> str:
    path_arg = _path_arg(args)
    uid, p = _resolve(path_arg)
    old, new = args.get("old_string", ""), args.get("new_string", "")
    diff = args.get("diff") or args.get("patch")
    if diff and (old or new):
        return ("error: pass either diff or old_string/new_string, not both - one edit per call "
                "(a diff can carry several hunks).")
    if not diff and not old:
        raise ValueError("old_string required")
    replace_all = bool(args.get("replace_all"))
    if not file_state.was_read(str(p)):
        if await _read_existing(uid, p) is None:   # a mistyped name, not an unread file: say so and name the real one
            return await _missing_msg(uid, p, path_arg, "Create it with write_file first.")
        return (f"error: read {path_arg} before editing it - call read_file (with offset/limit for the part "
                "you will change) so old_string is copied from the real text.")
    before = await _read_existing(uid, p)
    if before is None:
        raise FileNotFoundError(f"File not found: {path_arg}")
    if diff:
        big = _too_big(diff, "edit_file")
        if big:
            return big
        try:
            res = edit_engine.apply_diff(before, diff)
        except edit_engine.EditError as e:
            return f"error: {e}"
        after = res["text"]
        if args.get("pre_save_check", True) and not args.get("skip_verify"):
            from .verify import validate_code_syntax
            is_ok, err = validate_code_syntax(path_arg, after)
            if not is_ok:
                return f"error: pre-save syntax validation failed for {path_arg}: {err} (file was not modified)"
        await _store(uid, p, before, after)
        where = f"line {res['first_line']}" if res["first_line"] == res["last_line"] else \
            f"lines {res['first_line']}-{res['last_line']}"
        return f"edited {path_arg} at {where}: {res['hunks']} hunk(s) from diff\n{res['snippet']}" + \
            await verify_after(uid, p, path_arg, after, before, can_autorevert=True)
    try:
        res = edit_engine.apply_edit(before, old, new, replace_all)
    except edit_engine.EditError as e:
        return f"error: {e}"
    after = res["text"]
    if args.get("pre_save_check", True) and not args.get("skip_verify"):
        from .verify import validate_code_syntax
        is_ok, err = validate_code_syntax(path_arg, after)
        if not is_ok:
            return f"error: pre-save syntax validation failed for {path_arg}: {err} (file was not modified)"
    await _store(uid, p, before, after)
    where = f"line {res['first_line']}" if res["first_line"] == res["last_line"] else \
        f"lines {res['first_line']}-{res['last_line']}"
    note = {"whitespace": " (matched ignoring whitespace differences; indentation adjusted)",
            "fuzzy": " (block had drifted; matched the closest text and indentation adjusted - verify this line)"}.get(
        res["method"], "")
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
    if args.get("pre_save_check", True) and not args.get("skip_verify"):
        from .verify import validate_code_syntax
        is_ok, err = validate_code_syntax(path_arg, res["text"])
        if not is_ok:
            return f"error: pre-save syntax validation failed for {path_arg}: {err} (file was not modified)"
    await _store(uid, p, before, res["text"])
    msg = f"inserted {res['last_line'] - res['first_line'] + 1} line(s) at line {res['first_line']} of {path_arg}\n{res['snippet']}"
    return msg + await verify_after(uid, p, path_arg, res["text"], before, can_autorevert=True)


async def tool_run_python(args: dict) -> str:
    code = args.get("code")
    path_arg = args.get("path") or args.get("file") or args.get("filename")
    has_code = isinstance(code, str) and bool(code.strip())
    has_path = bool(path_arg)
    if has_code == has_path:
        raise ValueError("run_python requires exactly one of path or code")
    req_fn = getattr(_pkg(), "require_device_workspace", require_device_workspace)
    uid, ws = req_fn()
    cb = getattr(_pkg(), "companion_bridge", companion_bridge)
    inline = has_code
    if inline:
        # one script per call: concurrent runs sharing a workspace must never share
        # a filename. Failed scripts remain at this path so edit_file can repair them.
        script_name = f"_agent_run_{uuid.uuid4().hex[:8]}.py"
        script = ws / script_name
        source = code
        await cb.call(uid, "fs.write", {"path": str(script), "content": source, "append": False})
    else:
        uid, script = _resolve(str(path_arg))
        if script.suffix.lower() != ".py":
            # a truncated name (extension lost): point at the script we wrote, but never run it unasked - the user's
            # approval covers the exact call, so the retry must carry the exact name
            near = [n for n in await _suggest_names(uid, script) if n.lower().endswith(".py")]
            hint = f" Did you mean: {', '.join(near)}? Call run_python again with that exact name." if near else ""
            return f"error: run_python path must name a .py file ('{path_arg}').{hint}"
        source = await _read_existing(uid, script)
        if source is None:
            return await _missing_msg(uid, script, path_arg, "Write it first, then rerun run_python with that path.")
        script_name = str(path_arg).replace("\\", "/")
        file_state.mark_read(str(script))
    raw_t = APP_CONFIG.get("agent", {}).get("exec_timeout_s", 0)
    from ..shell_tools import DEFAULT_EXEC_TIMEOUT_S, python_approval_key, take_code_approval
    timeout = int(raw_t) if raw_t and int(raw_t) > 0 else DEFAULT_EXEC_TIMEOUT_S
    try:
        data = await cb.call(
            uid, "python.run", {"path": str(script), "cwd": str(ws), "timeout": timeout,
                                "source": source, "approved_in_app": take_code_approval(python_approval_key(args))},
            timeout=(timeout or 60) + 10)
    except TimeoutError:
        data = None
    succeeded = data is not None and data.get("exit_code") == 0
    retained = inline and not succeeded
    if inline and succeeded:
        try:
            await cb.call(uid, "fs.remove", {"path": str(script)})
        except Exception:
            retained = True
    if retained:
        file_state.mark_read(str(script))
        file_state.push_undo(str(script), None)
        _record_diff(script, None, source)
    retained_note = (f"\nscript retained at {script_name}; edit it with edit_file(path=\"{script_name}\"), "
                     f"then rerun with run_python(path=\"{script_name}\").") if retained else ""
    if data is None:
        return f"error: timed out after {timeout}s (config agent.exec_timeout_s){retained_note}"
    out = (data.get("stdout") or "")[-MAX_TOOL_OUTPUT:]
    err = (data.get("stderr") or "")[-4000:]
    result = f"exit code {data.get('exit_code')}"
    if out:
        result += f"\n--- stdout ---\n{out}"
    if err:
        result += f"\n--- stderr ---\n{err}"
    return result + retained_note
