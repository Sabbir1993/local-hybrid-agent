import asyncio
import inspect
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .db import _projects_db
from .small_model import APP_CONFIG, WORKSPACE_ROOT, COMMON_ROOT, describe_image_file
from .request_context import set_current_user, get_current_user_id  # noqa: F401 (re-exported)
from . import companion_bridge

MAX_TOOL_OUTPUT = 20000   # chars per tool result fed back to the model
MAX_EDIT_BYTES = 512 * 1024
# Both keyed by user_id (via request_context) rather than a single bare
# global -- this is a shared multi-user server, so "the active project" and
# "the tracked edits" must never leak across two users' concurrent requests.
_active_project: dict = {}   # user_id -> project name (set via UI)
_ws_changes: dict = {}       # user_id -> {path: {"before": str|None, "after": str|None}}


def get_active_project() -> Optional[str]:
    return _active_project.get(get_current_user_id())


def set_active_project(name: Optional[str]) -> None:
    _active_project[get_current_user_id()] = name
    _ws_changes.pop(get_current_user_id(), None)


def project_workspace_dir(name: str, owner_user_id: Optional[int] = None) -> Optional[Path]:
    uid = owner_user_id if owner_user_id is not None else get_current_user_id()
    row = _projects_db.execute(
        "SELECT workspace_dir FROM projects WHERE name = ? AND user_id = ?", (name, uid)
    ).fetchone()
    if row and row["workspace_dir"]:
        if companion_bridge.is_connected(uid):
            # Path lives on the user's machine, not this server -- mkdir (and
            # any further resolution) happens there, via the companion.
            return Path(row["workspace_dir"])
        p = Path(row["workspace_dir"]).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p
    return None


def active_workspace() -> Path:
    uid = get_current_user_id()
    proj = _active_project.get(uid)
    if proj:
        custom = project_workspace_dir(proj, uid)
        if custom:
            return custom.resolve()
        base = (WORKSPACE_ROOT / f"user_{uid}") if uid is not None else WORKSPACE_ROOT
        p = (base / proj).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p
    return WORKSPACE_ROOT.resolve()


def _remote_uid() -> Optional[int]:
    """user_id if a companion is connected for the calling user, else None.

    Only project workspaces with an explicit custom workspace_dir (picked via
    the companion-aware folder browser) are treated as remote -- the
    fallback WORKSPACE_ROOT/user_{uid}/proj path is always a server-local
    directory and stays server-local even when a companion is connected.
    """
    uid = get_current_user_id()
    if uid is None or not companion_bridge.is_connected(uid):
        return None
    proj = _active_project.get(uid)
    if proj and project_workspace_dir(proj, uid) is not None:
        return uid
    return None


def common_workspace() -> Path:
    p = COMMON_ROOT.resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _common_resolve(rel: str) -> Path:
    common = common_workspace()
    clean = str(rel).strip().replace("\\", "/")

    common_str = str(common).replace("\\", "/")
    if clean.lower().startswith(common_str.lower()):
        clean = clean[len(common_str):].lstrip("/")

    prefixes = ["/common/", "common/", "/workspace/", "workspace/"]
    for prefix in prefixes:
        if clean.lower().startswith(prefix):
            clean = clean[len(prefix):]
            break

    clean = clean.lstrip("/\\")
    if not clean or clean == ".":
        return common

    p = (common / clean).resolve()
    try:
        p.relative_to(common)
    except ValueError:
        if not str(p).lower().startswith(str(common).lower()):
            raise PermissionError(f"path escapes common space: {rel}")
    return p


def _ws_resolve(rel: str) -> Path:
    ws = active_workspace().resolve()
    clean = str(rel).strip().replace("\\", "/")

    ws_str = str(ws).replace("\\", "/")
    if clean.lower().startswith(ws_str.lower()):
        clean = clean[len(ws_str):].lstrip("/")

    prefixes = ["/workspace/", "workspace/", "/root/workspace/", "./workspace/"]
    for prefix in prefixes:
        if clean.lower().startswith(prefix):
            clean = clean[len(prefix):]
            break

    clean = clean.lstrip("/\\")
    if not clean or clean == ".":
        return ws

    p = (ws / clean).resolve()
    try:
        p.relative_to(ws)
    except ValueError:
        if not str(p).lower().startswith(str(ws).lower()):
            raise PermissionError(f"path escapes workspace: {rel}")
    return p


async def tool_list_files(args: dict) -> str:
    ws = active_workspace()
    pat = (args.get("pattern") or "").strip() or "**/*"
    uid = _remote_uid()
    if uid is not None:
        data = await companion_bridge.call(uid, "fs.list", {"root": str(ws), "pattern": pat})
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
    p = _ws_resolve(path_arg)
    uid = _remote_uid()
    if uid is not None:
        data = await companion_bridge.call(uid, "fs.read", {"path": str(p)})
        data_text = data.get("content")
        if data_text is None:
            return f"error: File not found: '{path_arg}'. It does not exist yet. Use 'write_file' to create it."
        if len(data_text) > MAX_TOOL_OUTPUT:
            return data_text[:MAX_TOOL_OUTPUT] + f"\n... (truncated, {len(data_text)} chars total)"
        return data_text
    if not p.is_file():
        return f"error: File not found: '{path_arg}'. It does not exist yet. Use 'write_file' to create it."
    data = p.read_text(encoding="utf-8", errors="replace")
    if len(data) > MAX_TOOL_OUTPUT:
        return data[:MAX_TOOL_OUTPUT] + f"\n... (truncated, {len(data)} chars total)"
    return data


async def tool_grep(args: dict) -> str:
    import re as _re
    pat = (args.get("pattern") or args.get("query") or "").strip()
    if not pat:
        raise ValueError("pattern required")
    rx = _re.compile(pat, _re.IGNORECASE)
    ws = active_workspace()
    uid = _remote_uid()
    if uid is not None:
        data = await companion_bridge.call(uid, "fs.grep", {"root": str(ws), "pattern": pat})
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
    p = _ws_resolve(path_arg)
    content = args.get("content", "")
    if len(content) > MAX_EDIT_BYTES:
        raise ValueError("content too large")
    append = bool(args.get("append"))

    uid = _remote_uid()
    if uid is not None:
        data = await companion_bridge.call(
            uid, "fs.write", {"path": str(p), "content": content, "append": append})
        _snapshot_change_remote(p)
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


def _parse_tabular_text(content: str) -> list[list[str]]:
    lines = [l.strip() for l in content.strip().splitlines() if l.strip()]
    if not lines:
        return []

    # Check if markdown table or pipe-separated (require a header row followed
    # by a separator row like |---|---| so a stray "|" inside a cell's text,
    # e.g. "Male|Female", doesn't misfire this branch on ordinary CSV/tab data).
    if len(lines) >= 2 and "|" in lines[0] and re.match(r"^\|?[\s\-:|]+\|?$", lines[1]):
        rows = []
        for l in lines:
            if re.match(r"^\|?[\s\-:|]+\|?$", l):
                continue
            cells = [c.strip() for c in l.split("|")]
            if l.startswith("|") and cells and cells[0] == "":
                cells.pop(0)
            if l.endswith("|") and cells and cells[-1] == "":
                cells.pop()
            if cells:
                rows.append(cells)
        if rows:
            return rows

    # Sniff the delimiter (comma, tab, semicolon, or pipe) rather than
    # assuming comma - models frequently emit tab- or semicolon-separated
    # rows, which a comma-only csv.reader collapses into a single column.
    import csv
    import io
    header = lines[0]
    candidates = [",", "\t", ";", "|"]
    counts = {d: header.count(d) for d in candidates}
    best = max(counts, key=counts.get)
    if counts[best] > 0:
        try:
            reader = csv.reader(io.StringIO(content), delimiter=best)
            rows = [[c.strip() for c in row] for row in reader if any(c.strip() for c in row)]
            if rows and len(rows[0]) > 1:
                return rows
        except Exception:
            pass

    return [[l] for l in lines]


def _save_text_as_excel(p: Path, content: str) -> bool:
    try:
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Sheet1"
        rows = _parse_tabular_text(content)
        if not rows:
            return False
        for r_idx, row in enumerate(rows, start=1):
            for c_idx, val in enumerate(row, start=1):
                val_clean = str(val).strip()
                if val_clean.lower() == "true":
                    ws.cell(row=r_idx, column=c_idx, value=True)
                elif val_clean.lower() == "false":
                    ws.cell(row=r_idx, column=c_idx, value=False)
                else:
                    try:
                        if "." in val_clean:
                            ws.cell(row=r_idx, column=c_idx, value=float(val_clean))
                        else:
                            ws.cell(row=r_idx, column=c_idx, value=int(val_clean))
                    except ValueError:
                        ws.cell(row=r_idx, column=c_idx, value=val_clean)
        wb.save(str(p))
        return True
    except Exception as e:
        print(f"[_save_text_as_excel error: {e}]", file=sys.stderr)
        return False


def tool_write_file_common(args: dict) -> str:
    path_arg = args.get("path") or args.get("file") or args.get("filename")
    if not path_arg and args.get("content"):
        c_low = args["content"][:300].lower()
        if "<!doctype html" in c_low or "<html" in c_low:
            path_arg = "index.html"
        elif "def " in c_low or "import " in c_low:
            path_arg = "main.py"
        elif "|" in c_low:
            path_arg = "data.csv"
        else:
            path_arg = "output.txt"
    if not path_arg:
        raise ValueError("path required")

    # The chat module always generates a fresh file - it never overwrites a
    # previous one in place, regardless of what filename the model picked
    # (models frequently reuse the same/example name across turns). A short
    # unique id is concatenated onto the stem so every generation lands on
    # its own file; explicit in-place edits go through edit_file, not this tool.
    import uuid
    stem = Path(path_arg).stem
    suffix = Path(path_arg).suffix
    unique_path_arg = f"{stem}_{uuid.uuid4().hex[:8]}{suffix}"

    p = _common_resolve(unique_path_arg)
    p.parent.mkdir(parents=True, exist_ok=True)
    content = args.get("content", "")
    if len(content) > MAX_EDIT_BYTES:
        raise ValueError("content too large")
    existed = p.exists()

    # Handle .xlsx / .xls conversion if structured text data is provided
    if p.suffix.lower() in (".xlsx", ".xls"):
        saved = _save_text_as_excel(p, content)
        if saved:
            return f"Wrote Excel file to common space: {p.name} ({'overwrote' if existed else 'created'}). [DOWNLOAD: {p.name}]"

    p.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} chars to common space: {p.name} ({'overwrote' if existed else 'created'}). [DOWNLOAD: {p.name}]"


async def tool_edit_file(args: dict) -> str:
    path_arg = args.get("path") or args.get("file") or args.get("filename")
    if not path_arg:
        raise ValueError("path required")
    p = _ws_resolve(path_arg)
    old, new = args.get("old_string", ""), args.get("new_string", "")
    if not old:
        raise ValueError("old_string required")
    replace_all = bool(args.get("replace_all"))

    uid = _remote_uid()
    if uid is not None:
        data = await companion_bridge.call(
            uid, "fs.edit", {"path": str(p), "old_string": old, "new_string": new, "replace_all": replace_all})
        n = int(data.get("count") or 0)
        _snapshot_change_remote(p)
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


def tool_run_python(args: dict) -> str:
    code = args.get("code", "")
    if not code.strip():
        raise ValueError("code required")
    ws = active_workspace()
    ws.mkdir(parents=True, exist_ok=True)
    script = ws / "_agent_run.py"
    script.write_text(code, encoding="utf-8")
    raw_t = APP_CONFIG.get("agent", {}).get("exec_timeout_s", 0)
    timeout = int(raw_t) if raw_t and int(raw_t) > 0 else None
    try:
        proc = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True, text=True, timeout=timeout, cwd=str(ws))
        out = (proc.stdout or "")[-MAX_TOOL_OUTPUT:]
        err = (proc.stderr or "")[-4000:]
        result = f"exit code {proc.returncode}"
        if out:
            result += f"\n--- stdout ---\n{out}"
        if err:
            result += f"\n--- stderr ---\n{err}"
        return result
    except subprocess.TimeoutExpired:
        return f"error: timed out after {timeout}s (config agent.exec_timeout_s)"


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


def _snapshot_change_remote(p: Path) -> None:
    """Like _snapshot_change, but the file lives on the companion machine --
    there is no local 'before' content to read, so just mark it touched."""
    changes = _ws_changes.setdefault(get_current_user_id(), {})
    rec = changes.setdefault(str(p), {})
    rec.setdefault("before", None)
    rec["after"] = "written"


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


def tool_revert(args: dict) -> str:
    target = args.get("path", "")
    changes = _ws_changes.get(get_current_user_id()) or {}
    for path, rec in changes.items():
        if Path(path).name == target or path.endswith(target):
            before = rec.get("before")
            if before is None:
                Path(path).unlink(missing_ok=True)
            else:
                Path(path).write_text(before, encoding="utf-8")
            del changes[path]
            return f"reverted {target}"
    return f"error: no tracked change for {target}"


async def tool_analyze_image(args: dict) -> str:
    p = _ws_resolve(args["path"])
    if not p.is_file():
        raise FileNotFoundError(p)
    return await describe_image_file(p, args.get("question", "Describe this image in detail for a coding agent."))


async def tool_search_memory(args: dict) -> str:
    query = args.get("query", "")
    if not query:
        raise ValueError("query required")
    try:
        from .memory import ensure_indexed, search_memory_hybrid
        await ensure_indexed(max_age_s=900)
        results = await search_memory_hybrid(query, k=8, requesting_user_id=get_current_user_id())
    except Exception as e:
        return f"error: memory search unavailable: {type(e).__name__}: {e}"
    if not results:
        return "(no matches in project memory - the index may still be building)"
    lines = []
    for r in results:
        where = r["path"] if r["source"] == "workspace" else f"{r['path']} (past session)"
        snippet = " ".join(str(r["text"]).split())[:220]
        lines.append(f"[{where}] score {r['score']:.2f}\n  {snippet}")
    return "\n".join(lines)


# ---------------- structured plan tracking ----------------
_plan_session_id: Optional[int] = None  # set per /agent/run via set_plan_context()

_PLAN_STATUS_MARKS = {"pending": "☐", "in_progress": "⏳", "done": "✅", "failed": "❌"}


def set_plan_context(session_id) -> None:
    """Point the plan tools at the current agent session (called from routes/agent.py)."""
    global _plan_session_id
    try:
        _plan_session_id = int(session_id) if session_id is not None else None
    except (TypeError, ValueError):
        _plan_session_id = None


def get_plan_context() -> Optional[int]:
    return _plan_session_id


def _format_plan(items: list) -> str:
    if not items:
        return "(no plan tracked for this session yet — call create_plan)"
    lines = []
    for it in items:
        mark = _PLAN_STATUS_MARKS.get(it.get("status", "pending"), "☐")
        line = f"{it['ord']}. {mark} {it['text']}"
        if it.get("note"):
            line += f"  — {it['note']}"
        lines.append(line)
    return "\n".join(lines)


def tool_create_plan(args: dict) -> str:
    from .db import db_get_plan_items, db_set_plan_items
    if _plan_session_id is None:
        raise ValueError("no active session for plan tracking (session_id missing from /agent/run)")
    raw = args.get("items")
    if isinstance(raw, str):
        raw = [s.strip() for s in raw.replace(";", "\n").split("\n") if s.strip()]
    if not isinstance(raw, list) or not raw:
        raise ValueError("items must be a non-empty array of step description strings")
    texts = []
    for it in raw[:20]:
        t = str(it.get("text") or "").strip() if isinstance(it, dict) else str(it).strip()
        if t:
            texts.append(t)
    if not texts:
        raise ValueError("plan contained no usable step text")
    db_set_plan_items(_plan_session_id, texts)
    return f"Plan created with {len(texts)} steps:\n" + _format_plan(db_get_plan_items(_plan_session_id))


def tool_update_plan_item(args: dict) -> str:
    from .db import db_get_plan_items, db_set_plan_item_status
    if _plan_session_id is None:
        raise ValueError("no active session for plan tracking (session_id missing from /agent/run)")
    items = db_get_plan_items(_plan_session_id)
    if not items:
        raise ValueError("no plan exists yet — call create_plan first")
    try:
        no = int(args.get("item", 0))
    except (TypeError, ValueError):
        raise ValueError("'item' must be the 1-based step number")
    status = str(args.get("status") or "done").strip().lower()
    if status not in _PLAN_STATUS_MARKS:
        raise ValueError("status must be one of: pending, in_progress, done, failed")
    if not 1 <= no <= len(items):
        raise ValueError(f"item {no} out of range (plan has {len(items)} steps)")
    db_set_plan_item_status(_plan_session_id, no, status, str(args.get("note") or "").strip() or None)
    return f"Step {no} marked {status} {_PLAN_STATUS_MARKS[status]}.\n" + _format_plan(db_get_plan_items(_plan_session_id))


def tool_get_plan(args: dict) -> str:
    from .db import db_get_plan_items
    if _plan_session_id is None:
        raise ValueError("no active session for plan tracking (session_id missing from /agent/run)")
    return _format_plan(db_get_plan_items(_plan_session_id))


AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files, inspect directory contents, view folder structure, or see what files exist in the workspace. Pattern supports globs like * or **/*",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "glob pattern, e.g. * or **/* or **/*.py"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file's content from the workspace",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "workspace-relative path"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search all workspace files with a regex; returns file:line: match",
            "parameters": {
                "type": "object",
                "properties": {"pattern": {"type": "string", "description": "regex to search"}},
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file in the workspace. For large files (roughly 150+ lines), write it in several shorter calls: first call with append=false (or omitted) to create the file with the first chunk, then further calls with append=true to add the rest in order — this avoids output truncation/corruption on very long single-shot generations.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "append": {"type": "boolean", "description": "If true, append content to the end of the existing file instead of overwriting it. Use this to build a large file across multiple calls."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace an exact string in a file. old_string must match the file exactly and be unique (or set replace_all)",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    "replace_all": {"type": "boolean"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": "Run Python code in the active project workspace. Returns stdout/stderr. Use to test fixes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python source to execute"},
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_diff",
            "description": "List files the agent has created or modified this session",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "revert",
            "description": "Revert one file to its pre-session content",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_image",
            "description": "Describe an image file in the workspace using the vision model (screenshots, diagrams, UI captures)",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "question": {"type": "string", "description": "What to focus on, e.g. 'what error is shown?'"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_memory",
            "description": "Hybrid semantic + keyword search over workspace files and past sessions. Use for 'where did we...', 'how did we...', and finding relevant code by meaning.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_plan",
            "description": "Create the tracked task plan for this session: an ordered list of concrete steps. Call it once after exploring, before starting the work. Replaces any previous plan.",
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Ordered step descriptions, e.g. ['inspect config.py', 'add retry helper to client.py', 'verify with run_python']",
                    },
                },
                "required": ["items"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_plan_item",
            "description": "Mark one plan step as in_progress, done, or failed (by its 1-based step number). Call it immediately after each step finishes or fails.",
            "parameters": {
                "type": "object",
                "properties": {
                    "item": {"type": "integer", "description": "1-based step number from the plan"},
                    "status": {"type": "string", "enum": ["pending", "in_progress", "done", "failed"]},
                    "note": {"type": "string", "description": "optional short note, e.g. the error message"},
                },
                "required": ["item", "status"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_plan",
            "description": "Show the current plan with per-step status. Use it to re-orient after an interruption or before summarizing.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

AGENT_CORE_TOOLS = [
    t for t in AGENT_TOOLS
    if t["function"]["name"] in ("write_file", "read_file", "edit_file", "list_files", "run_python")
]

CHAT_WRITE_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "Create or write a file in the shared common space and share a downloadable link with the user. Use this whenever the user asks to generate, create, fill, or save data to an Excel (.xlsx), CSV, code, or document file.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "A filename that describes THIS file's actual content, with the correct extension for the format requested (e.g. quarterly_sales.xlsx, user_report.csv, fetch_data.py) - never reuse a name or extension from an earlier example or an earlier file in this conversation unless the user explicitly asked to edit that exact file."
                },
                "content": {
                    "type": "string",
                    "description": "The complete file content to write. For spreadsheets (.xlsx, .csv), provide formatted CSV rows or markdown table rows."
                }
            },
            "required": ["path", "content"]
        }
    }
}

TOOL_IMPLS = {
    "list_files": tool_list_files,
    "read_file": tool_read_file,
    "grep": tool_grep,
    "write_file": tool_write_file,
    "write_file_common": tool_write_file_common,
    "edit_file": tool_edit_file,
    "run_python": tool_run_python,
    "list_diff": tool_list_diff,
    "revert": tool_revert,
    "analyze_image": tool_analyze_image,
    "search_memory": tool_search_memory,
    "create_plan": tool_create_plan,
    "update_plan_item": tool_update_plan_item,
    "get_plan": tool_get_plan,
}

# Registered last, after AGENT_TOOLS/TOOL_IMPLS/active_workspace exist: core.subagent
# imports back from this module and from core.agent_loop, so wiring it in here (rather
# than at the top of the file) avoids a circular import during startup.
from .subagent import SPAWN_AGENT_SCHEMA, tool_spawn_agent  # noqa: E402
AGENT_TOOLS.append(SPAWN_AGENT_SCHEMA)
TOOL_IMPLS["spawn_agent"] = tool_spawn_agent
