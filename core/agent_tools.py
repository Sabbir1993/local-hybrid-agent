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

MAX_TOOL_OUTPUT = 20000   # chars per tool result fed back to the model
MAX_EDIT_BYTES = 512 * 1024
_active_project: Optional[str] = None  # current project name (set via UI)
_ws_changes: dict = {}                 # path -> {"before": str|None, "after": str|None}


def get_active_project() -> Optional[str]:
    return _active_project


def set_active_project(name: Optional[str]) -> None:
    global _active_project
    _active_project = name
    _ws_changes.clear()


def project_workspace_dir(name: str) -> Optional[Path]:
    for r in _projects_db.execute("SELECT workspace_dir FROM projects WHERE name = ?", (name,)):
        ws = r["workspace_dir"]
        if ws:
            p = Path(ws).resolve()
            p.mkdir(parents=True, exist_ok=True)
            return p
    return None


def active_workspace() -> Path:
    if _active_project:
        custom = project_workspace_dir(_active_project)
        if custom:
            return custom.resolve()
        p = (WORKSPACE_ROOT / _active_project).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p
    return WORKSPACE_ROOT.resolve()


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


def tool_list_files(args: dict) -> str:
    ws = active_workspace()
    pat = (args.get("pattern") or "").strip() or "**/*"
    hits = ws.glob(pat)
    out = []
    for h in sorted(hits)[:200]:
        if h.is_file():
            out.append(str(h.relative_to(ws)))
    return "\n".join(out) or "(no files matched)"


def tool_read_file(args: dict) -> str:
    path_arg = args.get("path") or args.get("file") or args.get("filename")
    if not path_arg:
        raise ValueError("path required")
    p = _ws_resolve(path_arg)
    if not p.is_file():
        return f"error: File not found: '{path_arg}'. It does not exist yet. Use 'write_file' to create it."
    data = p.read_text(encoding="utf-8", errors="replace")
    if len(data) > MAX_TOOL_OUTPUT:
        return data[:MAX_TOOL_OUTPUT] + f"\n... (truncated, {len(data)} chars total)"
    return data


def tool_grep(args: dict) -> str:
    import re as _re
    pat = (args.get("pattern") or args.get("query") or "").strip()
    if not pat:
        raise ValueError("pattern required")
    rx = _re.compile(pat, _re.IGNORECASE)
    ws = active_workspace()
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


def tool_write_file(args: dict) -> str:
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
    p.parent.mkdir(parents=True, exist_ok=True)
    content = args.get("content", "")
    if len(content) > MAX_EDIT_BYTES:
        raise ValueError("content too large")
    existed = p.exists()
    p.write_text(content, encoding="utf-8")
    _snapshot_change(p)
    return f"wrote {len(content)} chars to {path_arg} ({'overwrote' if existed else 'created'})"


def _parse_tabular_text(content: str) -> list[list[str]]:
    lines = [l.strip() for l in content.strip().splitlines() if l.strip()]
    if not lines:
        return []

    # Check if markdown table or pipe-separated
    if any("|" in l for l in lines):
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

    # Check if CSV
    import csv
    import io
    try:
        reader = csv.reader(io.StringIO(content))
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
    p = _common_resolve(path_arg)
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


def tool_edit_file(args: dict) -> str:
    path_arg = args.get("path") or args.get("file") or args.get("filename")
    if not path_arg:
        raise ValueError("path required")
    p = _ws_resolve(path_arg)
    if not p.is_file():
        raise FileNotFoundError(f"File not found: {path_arg}")
    text = p.read_text(encoding="utf-8", errors="replace")
    old, new = args.get("old_string", ""), args.get("new_string", "")
    if not old:
        raise ValueError("old_string required")
    n = text.count(old)
    if n == 0:
        raise FileNotFoundError(f"old_string not found in file: {path_arg}")
    if n > 1 and not args.get("replace_all"):
        raise ValueError(f"old_string appears {n}x - add replace_all or more context")
    text = text.replace(old, new) if args.get("replace_all") else text.replace(old, new, 1)
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
    key = str(p)
    rec = _ws_changes.setdefault(key, {})
    if "before" not in rec:
        try:
            rec["before"] = p.read_text(encoding="utf-8", errors="replace") if p.exists() else None
        except Exception:
            rec["before"] = None
    rec["after"] = "written"


def tool_list_diff(args: dict) -> str:
    if not _ws_changes:
        return "(no tracked changes in this session)"
    out = []
    for path, rec in _ws_changes.items():
        rel = Path(path).name
        status = "created" if rec.get("before") is None else "modified"
        out.append(f"{rel}: {status}")
    return "\n".join(out)


def tool_revert(args: dict) -> str:
    target = args.get("path", "")
    for path, rec in _ws_changes.items():
        if Path(path).name == target or path.endswith(target):
            before = rec.get("before")
            if before is None:
                Path(path).unlink(missing_ok=True)
            else:
                Path(path).write_text(before, encoding="utf-8")
            del _ws_changes[path]
            return f"reverted {target}"
    return f"error: no tracked change for {target}"


async def tool_analyze_image(args: dict) -> str:
    p = _ws_resolve(args["path"])
    if not p.is_file():
        raise FileNotFoundError(p)
    return await describe_image_file(p, args.get("question", "Describe this image in detail for a coding agent."))


def tool_search_memory(args: dict) -> str:
    query = args.get("query", "")
    if not query:
        raise ValueError("query required")
    results = []
    ws = active_workspace()
    import re as _re
    words = [w for w in _re.findall(r"\w{3,}", query)][:6]
    for f in ws.rglob("*"):
        if not f.is_file() or f.stat().st_size > 2_000_000:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        score = sum(text.lower().count(w.lower()) for w in words)
        if score > 0:
            results.append((score, str(f.relative_to(ws))))
    results.sort(reverse=True)
    if not results:
        return "(no matches in project memory)"
    return "\n".join(f"{r} (score {s})" for s, r in results[:10])


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
            "description": "Create or overwrite a file in the workspace",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
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
            "description": "Search workspace memory and past session titles",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
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
                    "description": "Filename or path (e.g. route_sample_file.xlsx, data.csv, script.py, output.txt)"
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
}
