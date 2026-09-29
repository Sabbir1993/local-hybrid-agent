import re as _re
import sys
from pathlib import Path

from .. import companion_bridge
from ..small_model import APP_CONFIG
from .diffs import _record_diff, _remote_read_or_none, _snapshot_change
from .workspace import (
    DOCUMENT_EXTS,
    MAX_EDIT_BYTES,
    MAX_TOOL_OUTPUT,
    _remote_uid,
    _ws_resolve,
    active_workspace,
    require_device_workspace,
)


def _pkg():
    return sys.modules.get("core.agent_tools")


async def tool_list_files(args: dict) -> str:
    ws_fn = getattr(_pkg(), "active_workspace", active_workspace)
    ws = ws_fn()
    pat = (args.get("pattern") or "").strip() or "**/*"
    uid_fn = getattr(_pkg(), "_remote_uid", _remote_uid)
    uid = uid_fn()
    cb = getattr(_pkg(), "companion_bridge", companion_bridge)
    if uid is not None:
        data = await cb.call(uid, "fs.list", {"root": str(ws), "pattern": pat})
        return "\n".join(data.get("files") or []) or "(no files matched)"
    hits = ws.glob(pat)
    out = []
    for h in sorted(hits)[:200]:
        if h.is_file():
            out.append(str(h.relative_to(ws)))
    return "\n".join(out) or "(no files matched)"


async def tool_read_file(args: dict) -> str:
    path_arg = args.get("path") or args.get("file") or args.get("filename")
    if not path_arg:
        raise ValueError("path required")
    res_fn = getattr(_pkg(), "_ws_resolve", _ws_resolve)
    p = res_fn(path_arg)
    uid_fn = getattr(_pkg(), "_remote_uid", _remote_uid)
    uid = uid_fn()
    cb = getattr(_pkg(), "companion_bridge", companion_bridge)
    if uid is not None:
        data = await cb.call(uid, "fs.read", {"path": str(p)})
        data_text = data.get("content")
        if data_text is None:
            return f"error: File not found: '{path_arg}'. It does not exist yet. Use 'write_file' to create it."
        if len(data_text) > MAX_TOOL_OUTPUT:
            return (data_text[:MAX_TOOL_OUTPUT]
                    + f"\n... (truncated, {len(data_text)} chars total - call "
                      f"read_file_chunk('{path_arg}', offset_chars={MAX_TOOL_OUTPUT}) "
                      f"to read the rest)")
        return data_text
    if not p.is_file():
        return f"error: File not found: '{path_arg}'. It does not exist yet. Use 'write_file' to create it."
    data = p.read_text(encoding="utf-8", errors="replace")
    if len(data) > MAX_TOOL_OUTPUT:
        return (data[:MAX_TOOL_OUTPUT]
                + f"\n... (truncated, {len(data)} chars total - call "
                  f"read_file_chunk('{path_arg}', offset_chars={MAX_TOOL_OUTPUT}) "
                  f"to read the rest)")
    return data


async def tool_grep(args: dict) -> str:
    pat = (args.get("pattern") or args.get("query") or "").strip()
    if not pat:
        raise ValueError("pattern required")
    rx = _re.compile(pat, _re.IGNORECASE)
    ws_fn = getattr(_pkg(), "active_workspace", active_workspace)
    ws = ws_fn()
    uid_fn = getattr(_pkg(), "_remote_uid", _remote_uid)
    uid = uid_fn()
    cb = getattr(_pkg(), "companion_bridge", companion_bridge)
    if uid is not None:
        data = await cb.call(uid, "fs.grep", {"root": str(ws), "pattern": pat})
        return "\n".join(data.get("hits") or []) or "(no matches)"
    hits = []
    for f in ws.rglob("*"):
        if not f.is_file() or f.stat().st_size > 2_000_000:
            continue
        try:
            for i, line in enumerate(f.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{f.relative_to(ws)}:{i}: {line.strip()[:200]}")
                    if len(hits) >= 100:
                        break
        except Exception:
            continue
        if len(hits) >= 100:
            break
    return "\n".join(hits) or "(no matches)"


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
    res_fn = getattr(_pkg(), "_ws_resolve", _ws_resolve)
    p = res_fn(path_arg)
    content = args.get("content", "")
    if len(content) > MAX_EDIT_BYTES:
        raise ValueError("content too large")
    append = bool(args.get("append"))

    uid_fn = getattr(_pkg(), "_remote_uid", _remote_uid)
    uid = uid_fn()
    cb = getattr(_pkg(), "companion_bridge", companion_bridge)
    if p.suffix.lower() in DOCUMENT_EXTS:
        if append:
            raise ValueError(
                f"cannot append to a {p.suffix.lower()} document - send the complete file in one "
                "write_file call, or change an existing document with doc_edit")
        from ..doc_tools import tool_doc_create
        return await tool_doc_create({"file": path_arg, "content": content})
    if uid is not None:
        before = await _remote_read_or_none(uid, p)
        data = await cb.call(
            uid, "fs.write", {"path": str(p), "content": content, "append": append})
        _record_diff(p, before, (before or "") + content if append else content)
        existed = bool(data.get("existed"))
        verb = "appended" if append and existed else "wrote"
        return f"{verb} {len(content)} chars to {path_arg} ({'overwrote' if existed and not append else 'created' if not existed else 'appended'})"

    p.parent.mkdir(parents=True, exist_ok=True)
    existed = p.exists()
    if append and existed:
        with p.open("a", encoding="utf-8") as f:
            f.write(content)
        _snapshot_change(p)
        return f"appended {len(content)} chars to {path_arg} (total {p.stat().st_size} bytes)"

    p.write_text(content, encoding="utf-8")
    _snapshot_change(p)
    return f"wrote {len(content)} chars to {path_arg} ({'overwrote' if existed else 'created'})"


async def tool_edit_file(args: dict) -> str:
    path_arg = args.get("path") or args.get("file") or args.get("filename")
    if not path_arg:
        raise ValueError("path required")
    res_fn = getattr(_pkg(), "_ws_resolve", _ws_resolve)
    p = res_fn(path_arg)
    old, new = args.get("old_string", ""), args.get("new_string", "")
    if not old:
        raise ValueError("old_string required")
    replace_all = bool(args.get("replace_all"))

    uid_fn = getattr(_pkg(), "_remote_uid", _remote_uid)
    uid = uid_fn()
    cb = getattr(_pkg(), "companion_bridge", companion_bridge)
    if uid is not None:
        before = await _remote_read_or_none(uid, p)
        data = await cb.call(
            uid, "fs.edit", {"path": str(p), "old_string": old, "new_string": new, "replace_all": replace_all})
        n = int(data.get("count") or 0)
        if before is not None:
            _record_diff(p, before, before.replace(old, new) if replace_all else before.replace(old, new, 1))
        return f"edited {path_arg} ({n} replacement(s))"

    if not p.is_file():
        raise FileNotFoundError(f"File not found: {path_arg}")
    text = p.read_text(encoding="utf-8", errors="replace")
    n = text.count(old)
    if n == 0:
        raise FileNotFoundError(f"old_string not found in file: {path_arg}")
    if n > 1 and not replace_all:
        raise ValueError(f"old_string appears {n}x - add replace_all or more context")
    text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    p.write_text(text, encoding="utf-8")
    _snapshot_change(p)
    return f"edited {path_arg} ({n} replacement(s))"


async def tool_run_python(args: dict) -> str:
    code = args.get("code", "")
    if not code.strip():
        raise ValueError("code required")
    req_fn = getattr(_pkg(), "require_device_workspace", require_device_workspace)
    uid, ws = req_fn()
    cb = getattr(_pkg(), "companion_bridge", companion_bridge)
    script = ws / "_agent_run.py"
    await cb.call(uid, "fs.write", {"path": str(script), "content": code, "append": False})
    raw_t = APP_CONFIG.get("agent", {}).get("exec_timeout_s", 0)
    from ..shell_tools import DEFAULT_EXEC_TIMEOUT_S, take_code_approval
    timeout = int(raw_t) if raw_t and int(raw_t) > 0 else DEFAULT_EXEC_TIMEOUT_S
    try:
        data = await cb.call(
            uid, "shell.run", {"command": 'python "_agent_run.py"', "cwd": str(ws), "timeout": timeout,
                               "display": code, "approved_in_app": take_code_approval(code)},
            timeout=(timeout or 60) + 10)
    except TimeoutError:
        return f"error: timed out after {timeout}s (config agent.exec_timeout_s)"
    out = (data.get("stdout") or "")[-MAX_TOOL_OUTPUT:]
    err = (data.get("stderr") or "")[-4000:]
    result = f"exit code {data.get('exit_code')}"
    if out:
        result += f"\n--- stdout ---\n{out}"
    if err:
        result += f"\n--- stderr ---\n{err}"
    return result
