import asyncio
import base64
import contextvars
import inspect
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .db import _projects_db
from .small_model import APP_CONFIG, COMMON_ROOT, describe_image_bytes
from .request_context import set_current_user, get_current_user_id, get_current_device_id  # noqa: F401 (re-exported)
from . import companion_bridge

MAX_TOOL_OUTPUT = 20000   # chars per tool result fed back to the model
MAX_EDIT_BYTES = 512 * 1024
# Binary document formats: always built by core.doc_ops (real layouts/styles, so a
# later doc_edit can change one part without regenerating the rest), never by
# writing model text straight into the container.
DOCUMENT_EXTS = (".xlsx", ".xls", ".pptx", ".ppt", ".docx", ".doc", ".pdf")
# Both keyed by (user_id, device_id) via request_context so active projects
# and workspace paths never conflict across different users or different devices.
_active_project: dict = {}   # "uid:did" -> project name (set via UI)
_ws_changes: dict = {}       # uid -> {path: {"before": str|None, "after": str|None}}


def _user_device_key(user_id: Optional[int] = None, device_id: Optional[str] = None) -> str:
    uid = user_id if user_id is not None else get_current_user_id()
    did = device_id if device_id is not None else get_current_device_id()
    return f"{uid}:{did or 'default'}"


def get_active_project(user_id: Optional[int] = None, device_id: Optional[str] = None) -> Optional[str]:
    k = _user_device_key(user_id, device_id)
    if k in _active_project:
        return _active_project[k]
    uid = user_id if user_id is not None else get_current_user_id()
    return _active_project.get(f"{uid}:default") or _active_project.get(uid)


def set_active_project(name: Optional[str], user_id: Optional[int] = None, device_id: Optional[str] = None) -> None:
    k = _user_device_key(user_id, device_id)
    _active_project[k] = name
    _ws_changes.pop(user_id if user_id is not None else get_current_user_id(), None)


class WorkspaceAccessDenied(PermissionError):
    """Agent workspace access refused: the server's own disk is never a workspace."""


def require_device_workspace() -> tuple[int, Path]:
    """(user_id, project folder on the user's machine) for the calling request.

    Policy: agent file/exec tools operate ONLY on the user's own device, via
    the companion app. There is deliberately no fallback to WORKSPACE_ROOT or a
    server sandbox -- every missing piece (no device header, no project on this
    device, no local folder, companion offline) raises instead. Lookups are
    strict on (user, device): no cross-device or "default" fallbacks, so a
    request that lost its X-Device-Id header can't resolve another path.
    """
    uid = get_current_user_id()
    did = get_current_device_id()
    if uid is None:
        raise WorkspaceAccessDenied("not signed in")
    proj = _active_project.get(_user_device_key(uid, did))
    if not proj:
        raise WorkspaceAccessDenied(
            "no project is selected on this device - select a project from your machine first")
    row = _projects_db.execute(
        "SELECT workspace_dir FROM projects WHERE name = ? AND user_id = ? AND device_id = ?",
        (proj, uid, did)).fetchone()
    if not row or not row["workspace_dir"]:
        raise WorkspaceAccessDenied(
            f"project '{proj}' has no folder on your machine - recreate it and pick a local folder")
    if not companion_bridge.is_available(uid):
        raise WorkspaceAccessDenied(
            "the A770 Companion app is not connected - open it on your machine and try again")
    # the connected companion must be the machine this project lives on; otherwise a
    # path chosen on laptop A would be opened on laptop B
    comp_dev = (companion_bridge.connection_info(uid) or {}).get("device_id")
    if comp_dev and comp_dev != did:
        raise WorkspaceAccessDenied(
            "the connected A770 Companion is on a different machine than this project")
    return uid, Path(row["workspace_dir"])


def active_workspace() -> Path:
    """The active project's folder on the user's machine (see require_device_workspace)."""
    return require_device_workspace()[1]


def workspace_label() -> Optional[str]:
    """Display-only: the device workspace path, or None when unavailable (never raises)."""
    try:
        return str(require_device_workspace()[1])
    except WorkspaceAccessDenied:
        return None


def _remote_uid() -> int:
    """user_id whose companion executes workspace ops. Always remote: raises
    WorkspaceAccessDenied rather than letting a caller fall back to server-local IO."""
    return require_device_workspace()[0]


def user_common_root(uid: int) -> Path:
    """COMMON_ROOT/user_<uid>: one user's generated files and uploads."""
    return COMMON_ROOT.resolve() / f"user_{int(uid)}"


def common_workspace() -> Path:
    """The calling user's private common space. Users never share a folder, so
    download/preview/edit resolution can't reach another user's files."""
    uid = get_current_user_id()
    if uid is None:
        raise PermissionError("not signed in")
    p = user_common_root(uid)
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
    if not _within(p, common.resolve()):
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
    if not _within(p, ws):
        raise PermissionError(f"path escapes workspace: {rel}")
    return p


def _within(p: Path, root: Path) -> bool:
    """p is root or inside it. Case-insensitive on Windows, and compared on a
    path-separator boundary so a sibling like ".../proj2" never passes as
    being inside ".../proj" (a plain string startswith() would let it)."""
    try:
        p.relative_to(root)
        return True
    except ValueError:
        pass
    rp = os.path.normcase(os.path.normpath(str(p)))
    rr = os.path.normcase(os.path.normpath(str(root)))
    return rp == rr or rp.startswith(rr.rstrip("/" + os.sep) + os.sep)


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
    if p.suffix.lower() in DOCUMENT_EXTS:
        if append:
            raise ValueError(
                f"cannot append to a {p.suffix.lower()} document - send the complete file in one "
                "write_file call, or change an existing document with doc_edit")
        # fs.write is UTF-8 text: build the real document and send its bytes instead
        from .doc_tools import tool_doc_create
        return await tool_doc_create({"file": path_arg, "content": content})
    if uid is not None:
        before = await _remote_read_or_none(uid, p)
        data = await companion_bridge.call(
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


_MD_SEP_RE = re.compile(r"^\|?[\s\-:|]+\|?$")

# fence languages that are code, not tabular data: a ```python block must never
# become spreadsheet rows (one code line per row)
_CODE_FENCE_LANGS = frozenset({
    "python", "py", "javascript", "js", "jsx", "typescript", "ts", "tsx",
    "bash", "sh", "shell", "powershell", "ps1", "zsh", "json", "xml", "html",
    "css", "yaml", "yml", "toml", "ini", "c", "cpp", "c++", "h", "java", "go",
    "rust", "ruby", "php", "lua", "diff", "console", "terminal",
})
_CODE_LINE_RE = re.compile(
    r"^\s*(?:def\s+\w+\s*\(|class\s+\w+|import\s+\w|from\s+\S+\s+import\s|"
    r"for\s+\S+\s+in\s|while\s+\S+\s*:|return\s+\S|print\s*\(|#include|"
    r"function\s+\w+\s*\(|const\s+\w+\s*=|let\s+\w+\s*=|require\s*\()", re.IGNORECASE)


def _fenced_blocks(content: str) -> list:
    """(language, body) for every ```-fenced block, in document order."""
    blocks, lines, i = [], content.splitlines(), 0
    while i < len(lines):
        m = re.match(r"^\s*```(.*)$", lines[i])
        if not m:
            i += 1
            continue
        lang = m.group(1).strip().lower()
        i += 1
        body = []
        while i < len(lines) and not re.match(r"^\s*```\s*$", lines[i]):
            body.append(lines[i])
            i += 1
        blocks.append((lang, "\n".join(body)))
        i += 1
    return blocks


def _looks_like_code(body: str) -> bool:
    hits = sum(1 for l in body.splitlines() if _CODE_LINE_RE.match(l))
    return hits >= 2


def _markdown_table_rows(lines: list) -> list:
    """The first real markdown table (header + |---| separator + rows) in `lines`.
    Prose around it is not data, and neither is the separator row itself."""
    for i in range(len(lines) - 1):
        if "|" not in lines[i] or not _MD_SEP_RE.match(lines[i + 1]):
            continue
        rows = []
        for l in lines[i:]:
            if _MD_SEP_RE.match(l):
                continue
            if "|" not in l:
                break                      # the table ends at the first non-pipe line
            cells = [c.strip() for c in l.split("|")]
            if l.startswith("|") and cells and cells[0] == "":
                cells.pop(0)
            if l.endswith("|") and cells and cells[-1] == "":
                cells.pop()
            if any(cells):
                rows.append(cells)
        if len(rows) >= 2 and len(rows[0]) > 1:
            return rows
    return []


def _split_delimited(text: str, delim: str) -> list:
    import csv
    import io
    try:
        return [[c.strip() for c in row]
                for row in csv.reader(io.StringIO(text), delimiter=delim)
                if any(c.strip() for c in row)]
    except Exception:
        return []


def _modal_column_rows(rows: list, min_cols: int = 2) -> list:
    """Rows sharing the most common column count (>= min_cols), in order. Lines
    that do not split the same way - surrounding prose - are dropped."""
    from collections import Counter
    tallies = Counter(len(r) for r in rows if len(r) >= min_cols)
    if not tallies:
        return []
    modal = tallies.most_common(1)[0][0]
    return [r for r in rows if len(r) == modal]


def _sniff_delimited(lines: list) -> list:
    """Best rows over comma / tab / semicolon / pipe, or [] when no delimiter
    turns at least two lines into a consistent multi-column table."""
    best: list = []
    for delim in (",", "\t", ";", "|"):
        rows = _modal_column_rows(_split_delimited("\n".join(lines), delim))
        if len(rows) >= 2 and len(rows) > len(best):
            best = rows
    return best


def _parse_tabular_text(content: str) -> list[list[str]]:
    """Spreadsheet rows taken from model output: the table, never the prose
    around it.

    Order: a fenced data block, then a markdown table anywhere in the reply, then
    delimited text (sniffed, and filtered to the rows that split consistently so
    surrounding sentences drop out), and only when nothing multi-column exists,
    every line as a single column.
    """
    lines = [l.strip() for l in content.strip().splitlines() if l.strip()]
    if not lines:
        return []
    blocks = _fenced_blocks(content)

    # 1. a ```csv / ```markdown / bare ``` data block - but never a code block
    for lang, body in blocks:
        if lang in _CODE_FENCE_LANGS or not body.strip() or _looks_like_code(body):
            continue
        body_lines = [l.strip() for l in body.splitlines() if l.strip()]
        rows = _markdown_table_rows(body_lines) or _sniff_delimited(body_lines)
        if rows:
            return rows

    # 2. a markdown table with a |---|---| separator, wherever it appears
    rows = _markdown_table_rows(lines)
    if rows:
        return rows

    # 3. delimited text: rows that do not split the same way are prose
    rows = _sniff_delimited(lines)
    if rows:
        return rows

    # 4. nothing multi-column: one column of lines, minus fence markers. A reply
    #    whose only body is code is an answer, not data - return [] so the caller
    #    reports "no tabular data" instead of saving one code line per row.
    if any(lang in _CODE_FENCE_LANGS or _looks_like_code(body) for lang, body in blocks):
        return []
    return [[l] for l in lines if not l.startswith("```")]


def _find_chromium_binary() -> Optional[str]:
    import shutil
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        shutil.which("msedge"),
        shutil.which("chrome"),
        shutil.which("chromium"),
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def _markdown_to_html_dom(content: str, title: str = "", is_slides: bool = False) -> str:
    def esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def inline_format(s: str) -> str:
        s = esc(s)
        s = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', s)
        s = re.sub(r'__(.+?)__', r'<strong>\1</strong>', s)
        s = re.sub(r'\*(.+?)\*', r'<em>\1</em>', s)
        s = re.sub(r'_(.+?)_', r'<em>\1</em>', s)
        s = re.sub(r'`([^`]+)`', r'<code class="inline-code">\1</code>', s)
        return s

    clean_title = title or "Document"
    lines = content.strip().splitlines()

    if is_slides:
        slides = []
        cur_slide = {"title": "", "lines": []}
        for l in lines:
            trimmed = l.strip()
            if trimmed.startswith("---") or (trimmed.startswith("# ") and (cur_slide["title"] or cur_slide["lines"])):
                if cur_slide["title"] or cur_slide["lines"]:
                    slides.append(cur_slide)
                cur_slide = {"title": trimmed[2:].strip() if trimmed.startswith("# ") else "", "lines": []}
            elif trimmed.startswith("# "):
                cur_slide["title"] = trimmed[2:].strip()
            else:
                cur_slide["lines"].append(l)
        if cur_slide["title"] or cur_slide["lines"]:
            slides.append(cur_slide)
        if not slides:
            slides = [{"title": clean_title, "lines": [content]}]

        slides_html = []
        for idx, s in enumerate(slides, 1):
            s_title = inline_format(s["title"] or f"Slide {idx}")
            body_parts = []
            in_ul = False
            for line in s["lines"]:
                t = line.strip()
                if not t:
                    if in_ul:
                        body_parts.append("</ul>")
                        in_ul = False
                    continue
                if t.startswith("- ") or t.startswith("* "):
                    if not in_ul:
                        body_parts.append('<ul class="slide-list">')
                        in_ul = True
                    body_parts.append(f"<li>{inline_format(t[2:])}</li>")
                elif t.startswith("## ") or t.startswith("### "):
                    if in_ul:
                        body_parts.append("</ul>")
                        in_ul = False
                    sub = t.lstrip("#").strip()
                    body_parts.append(f'<h3 class="slide-subtitle">{inline_format(sub)}</h3>')
                else:
                    if in_ul:
                        body_parts.append("</ul>")
                        in_ul = False
                    body_parts.append(f'<p class="slide-text">{inline_format(t)}</p>')
            if in_ul:
                body_parts.append("</ul>")

            slides_html.append(f"""
            <section class="slide">
              <div class="slide-header">
                <h2 class="slide-title">{s_title}</h2>
                <span class="slide-num">{idx:02d} / {len(slides):02d}</span>
              </div>
              <div class="slide-body">
                {''.join(body_parts)}
              </div>
              <div class="slide-footer">
                <span>{esc(clean_title)}</span>
                <span>Generated by Autonomous Agent</span>
              </div>
            </section>
            """)

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{esc(clean_title)}</title>
<style>
  @page {{
    size: 11in 6.1875in landscape;
    margin: 0;
  }}
  @media print {{
    body {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    padding: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    background: #0f172a;
    color: #f8fafc;
  }}
  .slide {{
    page-break-after: always;
    width: 11in;
    height: 6.1875in;
    padding: 40px 52px;
    display: flex;
    flex-direction: column;
    justify-content: flex-start;
    background: #0f172a;
    box-sizing: border-box;
    overflow: hidden;
  }}
  .slide:nth-child(even) {{
    background: #1e293b;
  }}
  .slide-header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 2px solid #3b82f6;
    padding-bottom: 12px;
    margin-bottom: 24px;
  }}
  .slide-title {{
    margin: 0;
    font-size: 28px;
    font-weight: 700;
    color: #60a5fa;
  }}
  .slide-num {{
    font-size: 13px;
    color: #94a3b8;
    font-weight: 600;
    background: rgba(255,255,255,0.08);
    padding: 4px 10px;
    border-radius: 6px;
  }}
  .slide-body {{
    flex: 1;
    display: flex;
    flex-direction: column;
    justify-content: flex-start;
    font-size: 16px;
    line-height: 1.6;
  }}
  .slide-subtitle {{
    color: #93c5fd;
    font-size: 19px;
    margin: 12px 0 8px 0;
  }}
  .slide-text {{
    color: #cbd5e1;
    margin: 0 0 12px 0;
    font-size: 16px;
  }}
  .slide-list {{
    margin: 8px 0 16px 0;
    padding-left: 24px;
  }}
  .slide-list li {{
    margin-bottom: 10px;
    color: #e2e8f0;
  }}
  .slide-footer {{
    margin-top: auto;
    padding-top: 12px;
    border-top: 1px solid rgba(255,255,255,0.1);
    font-size: 11px;
    color: #64748b;
    display: flex;
    justify-content: space-between;
  }}
  .inline-code {{
    background: rgba(255,255,255,0.1);
    color: #93c5fd;
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 14px;
  }}
</style>
</head>
<body>
{''.join(slides_html)}
</body>
</html>"""

    # Standard Document mode
    body_elements = []
    in_code_block = False
    code_lang = ""
    code_buf = []
    in_ul = False
    in_ol = False
    table_buf = []

    def flush_table():
        nonlocal table_buf
        if not table_buf:
            return
        rows = []
        for tr in table_buf:
            cells = [c.strip() for c in tr.split("|")]
            if tr.startswith("|") and cells and cells[0] == "":
                cells.pop(0)
            if tr.endswith("|") and cells and cells[-1] == "":
                cells.pop()
            if cells:
                rows.append(cells)
        table_buf = []
        if not rows:
            return
        
        has_header = False
        if len(rows) >= 2 and all(re.match(r"^[\s\-:]+$", c) for c in rows[1]):
            header_row = rows[0]
            data_rows = rows[2:]
            has_header = True
        else:
            header_row = []
            data_rows = rows

        tbl_html = ['<table class="doc-table">']
        if has_header:
            tbl_html.append("<thead><tr>")
            for h in header_row:
                tbl_html.append(f"<th>{inline_format(h)}</th>")
            tbl_html.append("</tr></thead>")
        if data_rows:
            tbl_html.append("<tbody>")
            for r in data_rows:
                tbl_html.append("<tr>")
                for c in r:
                    tbl_html.append(f"<td>{inline_format(c)}</td>")
                tbl_html.append("</tr>")
            tbl_html.append("</tbody>")
        tbl_html.append("</table>")
        body_elements.append("".join(tbl_html))

    for line in lines:
        trimmed = line.strip()

        if trimmed.startswith("```"):
            flush_table()
            if in_code_block:
                escaped_code = esc("\n".join(code_buf))
                body_elements.append(f'<pre class="code-block"><code>{escaped_code}</code></pre>')
                code_buf = []
                in_code_block = False
            else:
                in_code_block = True
                code_lang = trimmed[3:].strip()
            continue

        if in_code_block:
            code_buf.append(line)
            continue

        if trimmed.startswith("|") and trimmed.endswith("|"):
            if in_ul: body_elements.append("</ul>"); in_ul = False
            if in_ol: body_elements.append("</ol>"); in_ol = False
            table_buf.append(trimmed)
            continue
        elif table_buf:
            flush_table()

        if not trimmed:
            if in_ul: body_elements.append("</ul>"); in_ul = False
            if in_ol: body_elements.append("</ol>"); in_ol = False
            continue

        if trimmed.startswith("# "):
            if in_ul: body_elements.append("</ul>"); in_ul = False
            if in_ol: body_elements.append("</ol>"); in_ol = False
            htext = inline_format(trimmed[2:].strip())
            body_elements.append(f'<h1 class="doc-h1">{htext}</h1>')
        elif trimmed.startswith("## "):
            if in_ul: body_elements.append("</ul>"); in_ul = False
            if in_ol: body_elements.append("</ol>"); in_ol = False
            htext = inline_format(trimmed[3:].strip())
            body_elements.append(f'<h2 class="doc-h2">{htext}</h2>')
        elif trimmed.startswith("### "):
            if in_ul: body_elements.append("</ul>"); in_ul = False
            if in_ol: body_elements.append("</ol>"); in_ol = False
            htext = inline_format(trimmed[4:].strip())
            body_elements.append(f'<h3 class="doc-h3">{htext}</h3>')
        elif trimmed.startswith("---") or trimmed.startswith("***"):
            if in_ul: body_elements.append("</ul>"); in_ul = False
            if in_ol: body_elements.append("</ol>"); in_ol = False
            body_elements.append('<hr class="doc-hr"/>')
        elif trimmed.startswith("> "):
            if in_ul: body_elements.append("</ul>"); in_ul = False
            if in_ol: body_elements.append("</ol>"); in_ol = False
            qtext = inline_format(trimmed[2:].strip())
            body_elements.append(f'<blockquote class="doc-quote">{qtext}</blockquote>')
        elif trimmed.startswith("- ") or trimmed.startswith("* "):
            if in_ol: body_elements.append("</ol>"); in_ol = False
            if not in_ul:
                body_elements.append('<ul class="doc-list">')
                in_ul = True
            body_elements.append(f'<li>{inline_format(trimmed[2:].strip())}</li>')
        elif re.match(r'^\d+\.\s+', trimmed):
            if in_ul: body_elements.append("</ul>"); in_ul = False
            if not in_ol:
                body_elements.append('<ol class="doc-ordered">')
                in_ol = True
            m = re.match(r'^\d+\.\s+(.*)', trimmed)
            body_elements.append(f'<li>{inline_format(m.group(1).strip() if m else trimmed)}</li>')
        else:
            if in_ul: body_elements.append("</ul>"); in_ul = False
            if in_ol: body_elements.append("</ol>"); in_ol = False
            ptext = inline_format(trimmed)
            body_elements.append(f'<p class="doc-p">{ptext}</p>')

    if in_code_block and code_buf:
        escaped_code = esc("\n".join(code_buf))
        body_elements.append(f'<pre class="code-block"><code>{escaped_code}</code></pre>')
    if table_buf:
        flush_table()
    if in_ul:
        body_elements.append("</ul>")
    if in_ol:
        body_elements.append("</ol>")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{esc(clean_title)}</title>
<style>
  @page {{
    size: A4;
    margin: 20mm 18mm 20mm 18mm;
  }}
  @media print {{
    body {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    color: #1e293b;
    line-height: 1.65;
    font-size: 13.5px;
    margin: 0;
    padding: 0;
    background: #ffffff;
  }}
  .doc-container {{
    max-width: 820px;
    margin: 0 auto;
    padding: 24px;
  }}
  .doc-header-badge {{
    display: inline-block;
    background: #eff6ff;
    color: #2563eb;
    border: 1px solid #bfdbfe;
    font-size: 10.5px;
    font-weight: 600;
    padding: 3px 8px;
    border-radius: 4px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-bottom: 8px;
  }}
  .doc-h1 {{
    color: #0f172a;
    font-size: 26px;
    font-weight: 700;
    border-bottom: 2px solid #2563eb;
    padding-bottom: 10px;
    margin: 0 0 16px 0;
  }}
  .doc-h2 {{
    color: #1e40af;
    font-size: 17px;
    font-weight: 700;
    border-left: 4px solid #3b82f6;
    padding-left: 10px;
    margin: 24px 0 10px 0;
  }}
  .doc-h3 {{
    color: #334155;
    font-size: 14px;
    font-weight: 600;
    margin: 18px 0 6px 0;
  }}
  .doc-p {{
    color: #334155;
    margin: 0 0 12px 0;
  }}
  .doc-list, .doc-ordered {{
    margin: 6px 0 14px 0;
    padding-left: 22px;
    color: #334155;
  }}
  .doc-list li, .doc-ordered li {{
    margin-bottom: 6px;
  }}
  .doc-hr {{
    border: 0;
    height: 1px;
    background: #e2e8f0;
    margin: 20px 0;
  }}
  .doc-quote {{
    border-left: 4px solid #94a3b8;
    background: #f8fafc;
    color: #475569;
    padding: 8px 14px;
    margin: 14px 0;
    border-radius: 0 6px 6px 0;
    font-style: italic;
  }}
  .doc-table {{
    width: 100%;
    border-collapse: collapse;
    margin: 16px 0;
    font-size: 12.5px;
  }}
  .doc-table th {{
    background: #f1f5f9;
    color: #1e293b;
    font-weight: 600;
    text-align: left;
    padding: 8px 12px;
    border-bottom: 2px solid #cbd5e1;
    border-top: 1px solid #e2e8f0;
  }}
  .doc-table td {{
    padding: 8px 12px;
    border-bottom: 1px solid #e2e8f0;
    color: #475569;
  }}
  .doc-table tr:nth-child(even) td {{
    background: #f8fafc;
  }}
  .code-block {{
    background: #0f172a;
    color: #f8fafc;
    padding: 14px;
    border-radius: 6px;
    font-size: 12px;
    overflow-x: auto;
    margin: 14px 0;
  }}
  code {{
    font-family: "Cascadia Code", Consolas, Monaco, monospace;
  }}
  .inline-code {{
    background: #f1f5f9;
    color: #0f172a;
    padding: 2px 5px;
    border-radius: 4px;
    font-size: 12px;
  }}
  .doc-footer {{
    margin-top: 36px;
    padding-top: 12px;
    border-top: 1px solid #e2e8f0;
    font-size: 11px;
    color: #94a3b8;
    display: flex;
    justify-content: space-between;
  }}
</style>
</head>
<body>
<div class="doc-container">
  <span class="doc-header-badge">Autonomous Document Engine</span>
  {''.join(body_elements)}
  <div class="doc-footer">
    <span>Document: {esc(clean_title)}</span>
    <span>Official Document</span>
  </div>
</div>
</body>
</html>"""


# The HTML may be model-written (prompt-injectable), and this browser runs on the
# server: no scripts, no network, no file:// subresources (iframe of config/app.json,
# fetch of 127.0.0.1:8090 or cloud metadata). Only inline styles and data: images/fonts.
_PDF_CSP = ("default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; "
            "script-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'")
_DOCTYPE_RX = re.compile(r"^\s*<!doctype[^>]*>", re.IGNORECASE)


def _lock_down_html(html_content: str) -> str:
    # Our <head> goes first so the CSP applies before any of the document is parsed; a
    # later <html>/<head> from the content merges in and its <style>s still apply.
    body = _DOCTYPE_RX.sub("", html_content, count=1)
    return ('<!doctype html><html><head><meta charset="utf-8">'
            f'<meta http-equiv="Content-Security-Policy" content="{_PDF_CSP}"></head>' + body)


def _render_html_to_pdf(html_content: str, output_pdf_path: Path) -> bool:
    browser_bin = _find_chromium_binary()
    if not browser_bin:
        return False

    import shutil
    import tempfile
    abs_pdf = os.path.abspath(str(output_pdf_path))
    output_pdf_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as tf:
        tf.write(_lock_down_html(html_content))
        tmp_html = os.path.abspath(tf.name)
    # throwaway profile: never the service account's real browser cookies/logins
    profile_dir = tempfile.mkdtemp(prefix="pdfprof_")

    try:
        cmd = [
            browser_bin,
            "--headless=new",
            "--disable-gpu",
            "--no-pdf-header-footer",
            f"--user-data-dir={profile_dir}",
            "--blink-settings=scriptEnabled=false",
            "--host-resolver-rules=MAP * ~NOTFOUND",
            "--proxy-server=127.0.0.1:9",
            "--disable-extensions",
            "--no-first-run",
            f"--print-to-pdf={abs_pdf}",
            tmp_html
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        if res.returncode != 0:
            print(f"[_render_html_to_pdf] browser exited {res.returncode}: "
                  f"{(res.stderr or '').strip()[:300]}", file=sys.stderr)
            return False
        return _pdf_looks_valid(Path(abs_pdf))
    except Exception as e:
        print(f"[_render_html_to_pdf browser error: {e}]", file=sys.stderr)
        return False
    finally:
        try:
            if os.path.exists(tmp_html):
                os.remove(tmp_html)
        except Exception:
            pass
        shutil.rmtree(profile_dir, ignore_errors=True)


def _pdf_looks_valid(p: Path) -> bool:
    """True only for a real PDF: present, non-empty, headed %PDF-. A renderer that
    writes escaped HTML source or a truncated body into the .pdf path is a failure,
    not a deliverable."""
    try:
        return p.is_file() and p.stat().st_size > 0 and p.read_bytes()[:5] == b"%PDF-"
    except OSError:
        return False


def _save_text_or_markdown_as_pdf(p: Path, content: str) -> bool:
    try:
        # Content already is PDF bytes (str form, latin1-encoded by the caller)
        if isinstance(content, str) and content.startswith("%PDF-"):
            p.write_bytes(content.encode("latin1", errors="replace"))
            return _pdf_looks_valid(p)
        if isinstance(content, bytes) and content.startswith(b"%PDF-"):
            p.write_bytes(content)
            return _pdf_looks_valid(p)

        # An HTML document has to be rendered by the browser. Falling through to the
        # markdown path would only escape its markup and emit a PDF of the HTML
        # source - a wrong document reported as a correct one.
        c_low = content.strip().lower()
        if c_low.startswith("<!doctype html") or "<html" in c_low[:200]:
            if _render_html_to_pdf(content, p):
                return True
            reason = ("no Chrome/Edge is installed to render HTML to PDF"
                      if not _find_chromium_binary() else "the headless browser render failed")
            print(f"[_save_text_or_markdown_as_pdf] {reason}", file=sys.stderr)
            return False

        # Check if requested as slides or presentation
        stem_low = p.stem.lower()
        is_slides = "slide" in stem_low or "presentation" in stem_low or "deck" in stem_low or (content.count("\n# ") >= 2 and "---" in content)
        doc_title = p.stem.replace("_", " ").replace("-", " ").title()

        # Step 1: Generate modern, beautiful HTML DOM
        html_dom = _markdown_to_html_dom(content, title=doc_title, is_slides=is_slides)

        # Step 2: Render HTML DOM to PDF via Headless Chromium/Edge
        if _render_html_to_pdf(html_dom, p):
            return True

        # Step 3: ReportLab fallback for markdown when no browser is available

        # Fallback to ReportLab if headless browser is unavailable
        from reportlab.lib.pagesizes import letter
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib import colors

        doc = SimpleDocTemplate(
            str(p),
            pagesize=letter,
            rightMargin=45, leftMargin=45,
            topMargin=45, bottomMargin=45
        )
        styles = getSampleStyleSheet()
        normal = ParagraphStyle('CustomNormal', parent=styles['Normal'], fontSize=10, leading=14, textColor=colors.HexColor("#1e293b"))
        title_style = ParagraphStyle('CustomTitle', parent=styles['Heading1'], fontSize=18, leading=22, textColor=colors.HexColor("#0f172a"), spaceAfter=8)
        h2_style = ParagraphStyle('CustomH2', parent=styles['Heading2'], fontSize=13, leading=17, textColor=colors.HexColor("#1e40af"), spaceBefore=10, spaceAfter=6)
        h3_style = ParagraphStyle('CustomH3', parent=styles['Heading3'], fontSize=11, leading=15, textColor=colors.HexColor("#334155"), spaceBefore=6, spaceAfter=4)
        code_style = ParagraphStyle('CustomCode', parent=styles['Code'], fontSize=9, leading=12, textColor=colors.HexColor("#0f172a"), backColor=colors.HexColor("#f1f5f9"), spaceAfter=6)

        story = []
        lines = content.strip().splitlines()
        in_code_block = False
        code_buf = []

        for line in lines:
            trimmed = line.strip()
            if trimmed.startswith("```"):
                if in_code_block:
                    story.append(Paragraph("<br/>".join(code_buf), code_style))
                    code_buf = []
                    in_code_block = False
                else:
                    in_code_block = True
                continue
            if in_code_block:
                escaped = line.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                code_buf.append(escaped)
                continue
            if not trimmed:
                story.append(Spacer(1, 6))
                continue
            if trimmed.startswith("# "):
                text = trimmed[2:].strip().replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                story.append(Paragraph(f"<b>{text}</b>", title_style))
                story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#cbd5e1"), spaceBefore=2, spaceAfter=8))
            elif trimmed.startswith("## "):
                text = trimmed[3:].strip().replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                story.append(Paragraph(f"<b>{text}</b>", h2_style))
            elif trimmed.startswith("### "):
                text = trimmed[4:].strip().replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                story.append(Paragraph(f"<b>{text}</b>", h3_style))
            elif trimmed.startswith("- ") or trimmed.startswith("* "):
                text = trimmed[2:].strip().replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
                story.append(Paragraph(f"&bull; {text}", normal))
            elif re.match(r'^\d+\.\s+', trimmed):
                num_m = re.match(r'^(\d+\.\s+)(.*)', trimmed)
                num_prefix = num_m.group(1) if num_m else ""
                text = (num_m.group(2) if num_m else trimmed).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
                story.append(Paragraph(f"{num_prefix}{text}", normal))
            elif trimmed.startswith("---") or trimmed.startswith("***"):
                story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#e2e8f0"), spaceBefore=4, spaceAfter=6))
            else:
                text = trimmed.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
                story.append(Paragraph(text, normal))
                story.append(Spacer(1, 3))

        if in_code_block and code_buf:
            story.append(Paragraph("<br/>".join(code_buf), code_style))

        if not story:
            # nothing to render: a PDF containing a single "Empty Document" line is
            # a fabricated deliverable, so fail and let the caller report it
            print("[_save_text_or_markdown_as_pdf] content produced no document elements",
                  file=sys.stderr)
            return False

        doc.build(story)
        return _pdf_looks_valid(p)
    except Exception as e:
        print(f"[_save_text_or_markdown_as_pdf error: {e}]", file=sys.stderr)
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

    clean_name = Path(path_arg).name
    stem = Path(clean_name).stem
    suffix = Path(clean_name).suffix or ".txt"

    # Maintain descriptive file stem and append a unique identifier in format [filename-unique id]
    # so that media, PDFs, presentations, and datasets are NEVER duplicated or overwritten
    import uuid
    import re as _re
    clean_stem = _re.sub(r'([-_][0-9a-fA-F]{8})+$', '', stem)
    unique_id = uuid.uuid4().hex[:8]
    unique_path_arg = f"{clean_stem}-{unique_id}{suffix}"

    p = _common_resolve(unique_path_arg)
    p.parent.mkdir(parents=True, exist_ok=True)
    content = args.get("content", "")
    if len(content) > MAX_EDIT_BYTES:
        raise ValueError("content too large")

    from . import doc_ops
    from .doc_ops.base import DocOpError
    try:
        doc_ops._check_ops_for_pan([{"op": "write", "content": content}])
    except DocOpError as e:
        return f"error: write refused: {e}"
    sfx = suffix.lower()
    spec = None

    # Handle .xlsx / .xls conversion if structured text data is provided
    if sfx in (".xlsx", ".xls"):
        if not content.strip():
            return ("error: refusing to create an empty spreadsheet - provide the data rows. "
                    "A spreadsheet is never filled with placeholder rows.")
        if sfx == ".xls":
            # openpyxl writes OOXML only: naming zip bytes ".xls" is what makes Excel
            # warn about a mismatched extension, so publish the real format instead
            p = p.with_suffix(".xlsx")
            unique_path_arg = p.name
        try:
            data, spec = doc_ops.create(p.name, content)
        except DocOpError as e:
            return f"error: {e}"
        p.write_bytes(data)
        msg = f"Wrote Excel file to common space: {unique_path_arg}. [DOWNLOAD: {unique_path_arg}]"

    # PowerPoint / Word: built on real layouts and styles so doc_edit can later
    # change one slide or paragraph without regenerating the rest
    elif sfx in (".pptx", ".ppt", ".docx", ".doc"):
        if not content.strip():
            return ("error: refusing to create an empty document - provide the content "
                    "(markdown; slides split by '---' or a leading '# ').")
        if sfx in (".ppt", ".doc"):
            p = p.with_suffix(sfx + "x")
            unique_path_arg = p.name
        try:
            data, spec = doc_ops.create(p.name, content)
        except DocOpError as e:
            return f"error: {e}"
        p.write_bytes(data)
        kind = "PowerPoint presentation" if p.suffix == ".pptx" else "Word document"
        msg = f"Wrote {kind} to common space: {unique_path_arg}. [DOWNLOAD: {unique_path_arg}]"

    # Handle .pdf conversion if writing to a PDF file (via HTML DOM first)
    elif sfx == ".pdf":
        if not content.strip():
            return ("error: refusing to create an empty PDF - provide the document content "
                    "(markdown or an HTML document).")
        if not _save_text_or_markdown_as_pdf(p, content):
            if p.is_file():
                p.unlink()   # never leave a half-written or bogus container behind
            return ("error: the PDF could not be compiled (Chrome/Edge missing or the render "
                    "failed), so nothing was saved. Send the document as markdown instead of "
                    "HTML, or try again.")
        spec = content   # PDF edits patch this markdown and re-render
        msg = f"Wrote compiled PDF document to common space: {unique_path_arg}. [DOWNLOAD: {unique_path_arg}]"

    else:
        p.write_text(content, encoding="utf-8")
        msg = f"Wrote {len(content)} chars to common space: {unique_path_arg}. [DOWNLOAD: {unique_path_arg}]"

    if doc_ops.is_doc(p.name) and p.is_file():
        from . import doc_store
        from .doc_ops.base import sha256
        doc_store.register(get_current_user_id(), p.name, p.suffix.lstrip(".").lower(),
                           source_spec=spec, sha256=sha256(p.read_bytes()))
    return msg


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
        before = await _remote_read_or_none(uid, p)
        data = await companion_bridge.call(
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
    # runs on the user's machine via the companion -- never on the server
    uid, ws = require_device_workspace()
    script = ws / "_agent_run.py"
    await companion_bridge.call(uid, "fs.write", {"path": str(script), "content": code, "append": False})
    raw_t = APP_CONFIG.get("agent", {}).get("exec_timeout_s", 0)
    from .shell_tools import DEFAULT_EXEC_TIMEOUT_S   # 0 = default, never unbounded on the device
    timeout = int(raw_t) if raw_t and int(raw_t) > 0 else DEFAULT_EXEC_TIMEOUT_S
    try:
        data = await companion_bridge.call(
            # display: the companion shows (and checks against the file) the code itself, not the shim
            uid, "shell.run", {"command": 'python "_agent_run.py"', "cwd": str(ws), "timeout": timeout,
                               "display": code},
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


_file_diffs: dict = {}       # (uid, path) -> per-call diff summary, popped into the tool_result event
MAX_DIFF_LINES = 400


async def _remote_read_or_none(uid: int, p: Path) -> Optional[str]:
    """Current content of a file on the companion machine, or None if missing/unreadable."""
    try:
        data = await companion_bridge.call(uid, "fs.read", {"path": str(p)})
    except Exception:
        return None
    content = data.get("content")
    return content if isinstance(content, str) else None


def diff_summary(before: Optional[str], after: str) -> dict:
    """{created, added, removed, hunks, truncated} -- unified diff with 3 lines of context."""
    import difflib
    a = (before or "").splitlines()
    b = (after or "").splitlines()
    hunks, added, removed = [], 0, 0
    for ln in list(difflib.unified_diff(a, b, lineterm="", n=3))[2:]:
        if ln.startswith("@@"):
            hunks.append({"t": "@", "s": ln})
        elif ln.startswith("+"):
            added += 1
            hunks.append({"t": "+", "s": ln[1:]})
        elif ln.startswith("-"):
            removed += 1
            hunks.append({"t": "-", "s": ln[1:]})
        else:
            hunks.append({"t": " ", "s": ln[1:]})
    return {"created": before is None, "added": added, "removed": removed,
            "hunks": hunks[:MAX_DIFF_LINES], "truncated": len(hunks) > MAX_DIFF_LINES}


def _record_diff(p: Path, before: Optional[str], after: str) -> None:
    """Track a companion-side write: session baseline for the workspace panel,
    plus this call's own diff for the activity feed."""
    uid = get_current_user_id()
    rec = _ws_changes.setdefault(uid, {}).setdefault(str(p), {})
    if "before" not in rec:
        rec["before"] = before
    rec["after"] = after
    _file_diffs[(uid, str(p))] = diff_summary(before, after)


def pop_file_diff(args: dict) -> Optional[dict]:
    """The diff recorded by the last write_file/edit_file call on this path (once)."""
    path_arg = (args or {}).get("path") or (args or {}).get("file") or (args or {}).get("filename")
    if not path_arg:
        return None
    try:
        p = _ws_resolve(path_arg)
    except Exception:
        return None
    return _file_diffs.pop((get_current_user_id(), str(p)), None)


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


async def tool_revert(args: dict) -> str:
    target = args.get("path", "")
    # tracked files live on the user's machine -- restore through the companion
    uid = _remote_uid()
    changes = _ws_changes.get(uid) or {}
    for path, rec in changes.items():
        if Path(path).name == target or path.endswith(target):
            before = rec.get("before")
            if before is None:
                return (f"error: {target} was created this session; the companion cannot delete "
                        "files -- ask the user to remove it")
            await companion_bridge.call(uid, "fs.write", {"path": path, "content": before, "append": False})
            del changes[path]
            return f"reverted {target}"
    return f"error: no tracked change for {target}"


MAX_IMAGE_BYTES = 10 * 1024 * 1024


async def tool_analyze_image(args: dict) -> str:
    # Read through the companion: the path is on the user's device, never this server's disk.
    p = _ws_resolve(args["path"])
    data = await companion_bridge.call(_remote_uid(), "fs.read_b64", {"path": str(p)})
    if not data.get("data"):
        return f"error: File not found: '{args['path']}'"
    raw = base64.b64decode(data["data"])
    if len(raw) > MAX_IMAGE_BYTES:
        return f"error: image too large ({len(raw)} bytes, max {MAX_IMAGE_BYTES})"
    return await describe_image_bytes(raw, args.get("question", "Describe this image in detail for a coding agent."))


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


async def tool_search_knowledge_base(args: dict) -> str:
    query = args.get("query", "")
    if not query:
        raise ValueError("query required")
    from .knowledge_access import KB_CLOUD_BLOCKED_MSG, kb_cloud_blocked
    if kb_cloud_blocked():
        return KB_CLOUD_BLOCKED_MSG
    uid = get_current_user_id()
    from .auth import _to_principal
    from .knowledge_access import allowed_source_ids_for
    user = _to_principal(uid) if uid is not None else None
    kb_ids = allowed_source_ids_for(user)
    if not kb_ids:
        return "(no accessible company knowledge base sources for this user account)"
    from .knowledge_router import fetch_company_knowledge
    hits, _ = await fetch_company_knowledge(query, allowed_source_ids=kb_ids, k=6)
    if not hits:
        return f"(no matching company knowledge base records found for '{query}')"
    lines = []
    for h in hits:
        title = h.get("title") or f"Source #{h.get('source_id')}"
        lines.append(f"[{title}] (score: {h['score']:.2f})\n{h['text']}")
    return "\n\n---\n\n".join(lines)


# ---------------- structured plan tracking ----------------
# per-request (contextvar) so concurrent /agent/run calls never share a plan
_plan_session_var: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "plan_session_id", default=None)

_PLAN_STATUS_MARKS = {"pending": "☐", "in_progress": "⏳", "done": "✅", "failed": "❌"}


def set_plan_context(session_id) -> None:
    """Point the plan tools at the current agent session (called from routes/agent.py)."""
    try:
        _plan_session_var.set(int(session_id) if session_id is not None else None)
    except (TypeError, ValueError):
        _plan_session_var.set(None)


def get_plan_context() -> Optional[int]:
    return _plan_session_var.get()


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
    if get_plan_context() is None:
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
    db_set_plan_items(get_plan_context(), texts)
    return f"Plan created with {len(texts)} steps:\n" + _format_plan(db_get_plan_items(get_plan_context()))


def tool_update_plan_item(args: dict) -> str:
    from .db import db_get_plan_items, db_set_plan_item_status
    if get_plan_context() is None:
        raise ValueError("no active session for plan tracking (session_id missing from /agent/run)")
    items = db_get_plan_items(get_plan_context())
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
    db_set_plan_item_status(get_plan_context(), no, status, str(args.get("note") or "").strip() or None)
    return f"Step {no} marked {status} {_PLAN_STATUS_MARKS[status]}.\n" + _format_plan(db_get_plan_items(get_plan_context()))


def tool_get_plan(args: dict) -> str:
    from .db import db_get_plan_items
    if get_plan_context() is None:
        raise ValueError("no active session for plan tracking (session_id missing from /agent/run)")
    return _format_plan(db_get_plan_items(get_plan_context()))


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
    {
        "type": "function",
        "function": {
            "name": "search_knowledge_base",
            "description": "Search the organizational company knowledge base (internal company documents, employee records, policies, services, guidelines). Returns authentic company records.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search term or question to search the company knowledge base for (e.g. 'engineering managers', 'leave policy', 'MIOT benefits', 'company overview')"
                    }
                },
                "required": ["query"],
            },
        },
    },
]

AGENT_CORE_TOOLS = [
    t for t in AGENT_TOOLS
    if t["function"]["name"] in ("write_file", "read_file", "edit_file", "list_files", "run_python", "search_knowledge_base")
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
    "search_knowledge_base": tool_search_knowledge_base,
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

# Document tools (doc_inspect / doc_edit / doc_create): same late wiring, since
# core.doc_tools imports the workspace helpers from this module.
from .doc_tools import AGENT_IMPLS as _DOC_IMPLS, DOC_SCHEMAS as _DOC_SCHEMAS  # noqa: E402
AGENT_TOOLS.extend(_DOC_SCHEMAS)
AGENT_CORE_TOOLS.extend(s for s in _DOC_SCHEMAS if s["function"]["name"] in ("doc_inspect", "doc_edit"))
TOOL_IMPLS.update(_DOC_IMPLS)
