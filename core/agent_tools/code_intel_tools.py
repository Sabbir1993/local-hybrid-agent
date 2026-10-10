"""Symbol-aware code navigation: find_symbol, find_references, file_outline.

grep answers "where does this string occur"; these answer "where is this symbol defined, who calls it, what
is in this file" - with line numbers and without reading bodies into context. They sit on the tree-sitter
index (core/code_intel) that is built from sources fetched from the user's device, in memory only.

Scope honesty, repeated in every result that needs it: references are syntactic call sites (a name match on
a call), not type-resolved usages. Imports, attribute reads and dynamic dispatch are not included - use grep.
"""

import difflib
import sys

from ..code_intel import ast_index, remote
from . import workspace as _workspace
from .workspace import _remote_uid, active_workspace

MAX_RESULTS = 40
_LANG_NOTE = "Indexed: Python, JavaScript, TypeScript (.py .js .jsx .mjs .cjs .ts .tsx)."


def _pkg():
    return sys.modules.get("core.agent_tools")


def _ws():
    return getattr(_pkg(), "active_workspace", active_workspace)()


def _uid():
    return getattr(_pkg(), "_remote_uid", _remote_uid)()


async def _synced():
    """(SyncResult, notes) for the active workspace; raises a readable error when the device cannot serve it."""
    root = str(_ws())
    try:
        res = await remote.sync(_uid(), root)
    except remote.CompanionTooOld:
        raise RuntimeError("the connected SSL Local Agent is too old for code navigation (no fs.read_many) - "
                           "update it, or use grep and read_file meanwhile")
    notes = []
    if res.truncated:
        notes.append(f"index covers the first {remote.MAX_FILES} files only")
    elif not res.complete:
        notes.append("index is still incomplete (large workspace) - results may miss files")
    if res.skipped_large:
        notes.append(f"{res.skipped_large} file(s) over {remote.MAX_FILE_BYTES // 1024} KB were not indexed")
    return res, notes


def _rel(path_arg: str) -> str:
    """Workspace-relative posix path for an argument that may be absolute, prefixed, or already relative."""
    p = _workspace._ws_resolve(path_arg)
    ws = _ws().resolve()
    try:
        return p.relative_to(ws).as_posix()
    except ValueError:
        s = str(p).replace("\\", "/")
        w = str(ws).replace("\\", "/").rstrip("/") + "/"
        return s[len(w):] if s.lower().startswith(w.lower()) else s.lstrip("/")


def _footer(notes: list) -> str:
    return ("\n(" + "; ".join(notes) + ")") if notes else ""


def _one_line(text: str, limit: int = 90) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _label(s: dict) -> str:
    qual = f"{s['class']}.{s['name']}" if s.get("class") else s["name"]
    return f"{qual}{s.get('signature') or ''}"


def _suggest(data: dict, name: str) -> list:
    names = sorted({s["name"] for s in data["symbols"]})
    low = name.lower()
    near = difflib.get_close_matches(name, names, n=5, cutoff=0.7)
    near += [n for n in names if low in n.lower() and n not in near][:5]
    return near[:6]


def _no_code_note(data: dict) -> str:
    return f"no indexable code found in the workspace. {_LANG_NOTE} Use grep for other file types."


async def tool_find_symbol(args: dict) -> str:
    name = str(args.get("name") or args.get("symbol") or "").strip()
    if not name:
        raise ValueError("name required")
    hint = str(args.get("path") or "").strip() or None
    res, notes = await _synced()
    data = res.index.data()
    if not data["files"]:
        return _no_code_note(data)
    cls = None
    look = name
    if "." in name:                     # Class.method
        cls, look = name.rsplit(".", 1)
    hits = ast_index.definitions_in(data, look, path_hint=hint.replace("\\", "/") if hint else None)
    if cls:
        hits = [h for h in hits if h.get("class") == cls]
    if not hits:
        sugg = _suggest(data, look)
        msg = f"no definition of '{name}'" + (f" under {hint}" if hint else "") + "."
        if sugg:
            msg += " Similar names: " + ", ".join(sugg) + "."
        return msg + " Use grep if it may be defined dynamically or in an unindexed file." + _footer(notes)
    lines = [f"{len(hits)} definition{'s' if len(hits) != 1 else ''} of '{name}':"]
    for h in hits[:MAX_RESULTS]:
        doc = f"  - {_one_line(h['docstring'])}" if h.get("docstring") else ""
        lines.append(f"{h['file']}:{h['line']}-{h['end_line']}  {h['kind']}  {_label(h)}{doc}")
    if len(hits) > MAX_RESULTS:
        lines.append(f"({len(hits) - MAX_RESULTS} more - narrow with path)")
    lines.append("Read a definition with read_file(path, offset=<line>, limit=<lines>).")
    return "\n".join(lines) + _footer(notes)


async def tool_find_references(args: dict) -> str:
    name = str(args.get("name") or args.get("symbol") or "").strip()
    if not name:
        raise ValueError("name required")
    look = name.rsplit(".", 1)[-1]
    res, notes = await _synced()
    data = res.index.data()
    if not data["files"]:
        return _no_code_note(data)
    calls = ast_index.callers_in(data, look)
    if not calls:
        defs = ast_index.definitions_in(data, look)
        where = (f" It is defined at {defs[0]['file']}:{defs[0]['line']} but nothing in the indexed code calls it."
                 if defs else f" No definition of '{look}' was found either.")
        return (f"no call sites of '{look}'.{where} References that are not calls (imports, attribute reads, "
                "string names) are not indexed - use grep." + _footer(notes))
    lines = [f"{len(calls)} call site{'s' if len(calls) != 1 else ''} of '{look}' (syntactic calls only):"]
    for c in calls[:MAX_RESULTS]:
        who = f"  in {c['caller']}" if c.get("caller") else "  at module level"
        lines.append(f"{c['file']}:{c['line']}{who}")
    if len(calls) > MAX_RESULTS:
        lines.append(f"({len(calls) - MAX_RESULTS} more call sites not shown)")
    return "\n".join(lines) + _footer(notes)


async def tool_file_outline(args: dict) -> str:
    path_arg = str(args.get("path") or args.get("file") or "").strip()
    if not path_arg:
        raise ValueError("path required")
    rel = _rel(path_arg)
    res, notes = await _synced()
    status = res.index.status(rel)
    if status is None:
        return (f"error: '{path_arg}' is not indexed (missing, over {remote.MAX_FILE_BYTES // 1024} KB, in a "
                f"skipped folder such as node_modules/dist/vendor, or not a supported type). {_LANG_NOTE}")
    if status == "parse_error":
        return f"error: '{path_arg}' has syntax errors, so its structure cannot be read - use read_file."
    items = res.index.outline(rel) or []
    if not items:
        return f"{rel}: no functions or classes defined."
    lines = [f"{rel}: {len(items)} symbols"]
    for s in items:
        indent = "  " if s.get("class") else ""
        doc = f"  - {_one_line(s['docstring'])}" if s.get("docstring") else ""
        lines.append(f"{indent}{s['line']}-{s['end_line']}  {s['kind']}  {s['name']}{s.get('signature') or ''}{doc}")
    return "\n".join(lines) + _footer(notes)


REPO_MAP_FILES = 12
REPO_MAP_SYMBOLS = 6
REPO_MAP_TIMEOUT_S = 8


async def repo_map(prefix: str = "") -> str:
    """A few lines naming the files that define the most, for project_overview. "" when the index is not
    available (old companion, nothing indexable, slow first sync): an overview must never fail on it. A sync
    cut short keeps what it fetched, so the next call is warmer."""
    import asyncio
    try:
        res, _ = await asyncio.wait_for(_synced(), REPO_MAP_TIMEOUT_S)
    except Exception:
        return ""
    prefix = prefix.strip("/")
    per_file: dict = {}
    for s in res.index.data()["symbols"]:
        if prefix and not s["file"].startswith(prefix + "/"):
            continue
        if s["kind"] != "method":
            per_file.setdefault(s["file"], []).append(s)
    if not per_file:
        return ""
    ranked = sorted(per_file.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:REPO_MAP_FILES]
    lines = ["Code map (files that define the most; use find_symbol / file_outline to go deeper):"]
    for rel, syms in ranked:
        shown = ", ".join(f"{s['name']}" for s in syms[:REPO_MAP_SYMBOLS])
        more = f" +{len(syms) - REPO_MAP_SYMBOLS} more" if len(syms) > REPO_MAP_SYMBOLS else ""
        lines.append(f"  {rel}: {shown}{more}")
    return "\n".join(lines)


CODE_INTEL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "find_symbol",
        "description": ("Find where a function, class or method is DEFINED (Python/JS/TS): file, line range, "
                        "signature. Faster and cheaper than grep + read_file. Accepts 'Class.method'."),
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "symbol name, e.g. 'authenticate' or 'Api.authenticate'"},
            "path": {"type": "string", "description": "optional file path (or suffix) to narrow the search"}},
            "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "find_references",
        "description": ("List every call site of a function/method (file:line and the calling function). "
                        "Calls only, not imports or attribute reads - use grep for those."),
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "function or method name"}},
            "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "file_outline",
        "description": ("Structure of one file: classes, functions and methods with line ranges and signatures, "
                        "without bodies. Use before read_file on a large file."),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "file path in the workspace"}},
            "required": ["path"]}}},
]

CODE_INTEL_IMPLS = {
    "find_symbol": tool_find_symbol,
    "find_references": tool_find_references,
    "file_outline": tool_file_outline,
}
CODE_INTEL_TOOL_NAMES = frozenset(CODE_INTEL_IMPLS)
