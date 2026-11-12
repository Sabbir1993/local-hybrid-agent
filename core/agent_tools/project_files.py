"""Chat project builder: a multi-file static site (html / css / js / json / md / svg) written in pieces, then one ZIP.

A reply is cut off by the output limit long before a whole website fits in one write_file call, so the model builds it the way a
person would: project_write one file at a time, project_append the next section of a long file, project_finish to get the ZIP and
a single-file preview. Nothing here runs: files are inert text in the user's own common space (never another user's), the same
folder the other chat documents use. Card numbers are refused on every write, like every other document the chat saves."""
import bisect
import contextvars
import html as _htmllib
from html.parser import HTMLParser
import json
import os
import re
import threading
import uuid
import zipfile
from pathlib import Path

from .workspace import MAX_EDIT_BYTES, _common_resolve, common_workspace

PROJECTS_DIR = "projects"
MAX_FILES = 60
MAX_PROJECT_BYTES = 6 * 1024 * 1024
TEXT_EXTS = (".html", ".htm", ".css", ".js", ".mjs", ".json", ".md", ".txt", ".svg", ".xml", ".csv", ".webmanifest")

_BAD_EXT = ("error: a project holds text files only (" + ", ".join(TEXT_EXTS) + "). Pictures and fonts are linked by address "
            "(https://...) or listed in an assets/README.md, never written here.")

PROJECT_WRITE_SCHEMA = {"type": "function", "function": {
    "name": "project_write",
    "description": ("Create or replace ONE file of a multi-file website / front-end project (index.html, css/styles.css, js/main.js, "
                    "README.md ...). Use it instead of write_file when the user wants a website or a project with several files, "
                    "and whenever a single file would be too long for one call: write a short skeleton first, then add the rest "
                    "with project_append. Keep each call under about 150 lines."),
    "parameters": {"type": "object", "properties": {
        "project": {"type": "string", "description": "short project name, the same in every call, e.g. dhaka-documentary"},
        "path": {"type": "string", "description": "relative file path inside the project, e.g. css/styles.css"},
        "content": {"type": "string", "description": "the complete text of this file (or of its skeleton)"},
        "replace": {"type": "boolean", "description": "true only to overwrite a file that is already complete, when the user asked to redo it"}},
        "required": ["project", "path", "content"]}}}

PROJECT_APPEND_SCHEMA = {"type": "function", "function": {
    "name": "project_append",
    "description": "Add the next section to the END of a file that project_write already created. Do not repeat what is there.",
    "parameters": {"type": "object", "properties": {
        "project": {"type": "string"}, "path": {"type": "string"},
        "content": {"type": "string", "description": "only the new text, under about 150 lines"}},
        "required": ["project", "path", "content"]}}}

PROJECT_FINISH_SCHEMA = {"type": "function", "function": {
    "name": "project_finish",
    "description": ("Call once when every file is written: checks the project and returns the ZIP of all files plus a single-file "
                    "preview of the entry page (CSS and JS inlined). Then reply with a short summary and the download links."),
    "parameters": {"type": "object", "properties": {
        "project": {"type": "string"},
        "entry": {"type": "string", "description": "the page to preview, default index.html"}},
        "required": ["project"]}}}

MAX_PLAN_FILES = 14

PROJECT_PLAN_SCHEMA = {"type": "function", "function": {
    "name": "project_plan",
    "description": ("Declare the files of the website BEFORE writing any of them: the complete list, at most 14 files, each with a "
                    "one-line purpose. Every tool result then shows how many are done and what is next, and project_finish refuses "
                    "until every planned file is written. Call it again to add or drop files if the plan changes."),
    "parameters": {"type": "object", "properties": {
        "project": {"type": "string", "description": "short project name, the same in every call"},
        "files": {"type": "array", "maxItems": MAX_PLAN_FILES, "items": {"type": "object", "properties": {
            "path": {"type": "string", "description": "relative path, e.g. js/charts.js"},
            "purpose": {"type": "string", "description": "one line: what this file holds"},
            "min_lines": {"type": "integer", "description": "rough size it must reach to count as done (default 5)"}},
            "required": ["path", "purpose"]}}},
        "required": ["project", "files"]}}}

PROJECT_TOOL_NAMES = ("project_plan", "project_write", "project_append", "project_finish")
PROJECT_SCHEMAS = [PROJECT_PLAN_SCHEMA, PROJECT_WRITE_SCHEMA, PROJECT_APPEND_SCHEMA, PROJECT_FINISH_SCHEMA]

PROJECT_MANUAL = (
    "WEBSITE / MULTI-FILE PROJECT (project_plan, project_write, project_append, project_finish): a whole site never fits in one reply, "
    "so plan it, then build it in pieces. 1) project_plan: the complete file list (at most 14) with a one-line purpose each. "
    "2) project_write each planned file, starting with index.html as a short skeleton, then css/styles.css and js/main.js. "
    "3) fill long files with project_append, one section per call, under about 150 lines each. Each tool result says how many planned "
    "files are done and which is next: follow it. 4) project_finish (it refuses while planned files are missing), then reply with a "
    "short summary and the two [DOWNLOAD: ...] links it returns. Use the same `project` name in every call. Link files with relative "
    "paths (css/styles.css, js/main.js). The page must read well without external libraries: CDN scripts are optional extras. "
    "Never write card numbers. Pictures are linked by https address, never invented as local files. Never paste code into the reply.")

SINGLE_PAGE_MANUAL = (
    "ONE HTML PAGE (Claude Artifacts standard): the user asked for a single html page, not a website. "
    "MATCH THE KIND OF PAGE. A report, guide, overview, comparison or \"detailed html\" about findings is a long-form READING page: a title with a one-paragraph summary, a table of contents, numbered sections with clear headings, tables for the facts, short callouts for the key risks and for each fix or solution, and the sources at the end. Build a dashboard (KPI cards, tabs, filters, charts) only when the user said dashboard or asked for charts or interactivity. "
    "Plan exactly ONE file, index.html, with "
    "Tailwind CSS CDN (<script src=\"https://cdn.tailwindcss.com\"></script>), Google Fonts (Plus Jakarta Sans), Lucide icons (<script src=\"https://unpkg.com/lucide@latest\"></script>), "
    "and CSS in a <style> and JS in a <script> inside it. Emit the complete, beautiful, production-ready page in ONE single project_write call "
    "(varying markup with modern cards and tables, and tabs or filters where they help; never paste repetitive placeholder cards). Then call project_finish immediately: "
    "it returns the one .html link. Do not create css/ or js/ files and do not mention a ZIP. Use project_append only if you need to extend an existing page. "
    "To fix or redo a page that already exists, write the complete updated page with project_write and replace=true. Never generate pictures for the page: "
    "link real photos by https address. In your final reply, copy the [DOWNLOAD: ...] link exactly as project_finish returned it.")

_WANTED = re.compile(
    r"\b(web ?site|web ?app|landing page|portfolio site|multi-?page|front-?end|single[- ]page|index\.html|styles\.css|main\.js|"
    r"project structure|html\s*,?\s*css|css\s*(?:and|,|&)\s*(?:vanilla\s*)?(?:js|javascript))\b", re.I)


_SLASH_CMD = re.compile(r"^\s*(?:/[\w:-]+\s+)+")      # "/frontend-design use this skill ..." is a command name, not words of the request
# "an html of Dhaka", "a single html page", "one html file": one page, unless the request also names real multi-file signals
_SINGLE_HTML = re.compile(r"\b(?:an?|one|single|1)\s+(?:[\w-]+\s+){0,4}html\b|\bhtml\s+(?:page|file|document|report|guide)\b", re.I)
_MULTI_FILE = re.compile(r"\b(web ?site|web ?app|multi-?page|index\.html|styles\.css|main\.js|project structure|zip|"
                         r"html\s*,?\s*css|css\s*(?:and|,|&)\s*(?:vanilla\s*)?(?:js|javascript))\b", re.I)


_MAKE = re.compile(r"\b(make|build|create|write|generate|design|produce|prepare|develop|give me|i (?:need|want))\b", re.I)


def single_page_wanted(query: str) -> bool:
    """A request to make ONE html page ("make a deep informative html of Dhaka"). It is still written in small pieces (one huge
    write_file call is cut off), but the user gets one .html, not a project plan and a ZIP."""
    q = _SLASH_CMD.sub("", str(query or ""))
    return bool(_SINGLE_HTML.search(q) and _MAKE.search(q) and not _MULTI_FILE.search(q))


def project_wanted(query: str) -> bool:
    """A request for a website or a multi-file front-end project (not a single html page or a report)."""
    q = _SLASH_CMD.sub("", str(query or ""))
    if _SINGLE_HTML.search(q) and not _MULTI_FILE.search(q):
        return False
    return bool(_WANTED.search(q))


def _slug(name) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower()).strip("-")[:40]
    return s or "project"


# Projects belong to the chat that made them: the same prompt in another chat starts from scratch, with its own plan, files and ZIP.
_scope_var: contextvars.ContextVar = contextvars.ContextVar("project_scope", default="anon")


def set_scope(session_id) -> None:
    """Called once per chat request (like set_current_user); tools run in a copy of this context. A chat that is not saved yet has no
    id: it gets a random one for this request only, so two unsaved chats of the same user never share (or see) each other's project."""
    try:
        _scope_var.set(f"s{int(session_id)}")
    except (TypeError, ValueError):
        _scope_var.set("u" + uuid.uuid4().hex[:10])


def reset_scope() -> None:
    """Back to the shared default scope (no chat): for tests and code that runs outside a chat request."""
    _scope_var.set("anon")


def _scope() -> str:
    return _scope_var.get()


def _root(project) -> Path:
    return _common_resolve(f"{PROJECTS_DIR}/{_scope()}/{_slug(project)}")


# One writer at a time per project (user + chat + slug): two tool calls of the same turn run in threads, and a read-modify-write of one
# file (append) would otherwise lose a piece. The root path already holds user, chat scope and slug.
_locks: dict = {}
_locks_guard = threading.Lock()


def _lock(project) -> "threading.RLock":
    key = str(_root(project))
    with _locks_guard:
        return _locks.setdefault(key, threading.RLock())


def _write_atomic(path: Path, text: str) -> None:
    """Write through a temp file in the same folder and swap it in, so a crash or a parallel reader never sees half a file."""
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}{_TMP_SUFFIX}")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


_STORAGE_ERROR = "error: the project files could not be saved on the server. Try again; if it keeps failing, use shorter plain file names."
_WIN_BAD = re.compile(r'[:<>"|?*\x00-\x1f]')
_WIN_RESERVED = re.compile(r"^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])$", re.I)


def _check_name(part: str) -> None:
    """One path segment must be a legal name on every system the ZIP is unpacked on (Windows is the strictest)."""
    if _WIN_BAD.search(part):
        raise ValueError('error: file and folder names must not contain : < > " | ? * or control characters')
    if part != part.rstrip(" ."):
        raise ValueError("error: a file or folder name must not end with a dot or a space")
    if _WIN_RESERVED.match(part.split(".", 1)[0].strip()):
        raise ValueError(f"error: `{part}` is a reserved device name on Windows; pick another file name")
    if part == _RECORD_NAME:
        raise ValueError("error: that file name is reserved; pick another one")


def _norm_parts(path) -> list:
    rel = str(path or "").replace("\\", "/").strip().lstrip("/")
    if rel.endswith("/"):
        raise ValueError("error: that is a folder; give a file path like css/styles.css (folders are created by writing files in them)")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or ".." in parts or re.match(r"^[A-Za-z]:", parts[0]) or len(parts) > 5:
        raise ValueError("error: path must be a relative file path inside the project, like css/styles.css")
    return parts


def _same_name_other_case(folder: Path, name: str):
    """The existing entry of `folder` that differs from `name` only by case (index.html vs INDEX.HTML), else None."""
    try:
        with os.scandir(folder) as it:
            for e in it:
                if e.name != name and e.name.lower() == name.lower():
                    return e.name
    except OSError:
        pass
    return None


def _file(project, path, plan_paths=None) -> tuple:
    """(project folder, target file, clean relative path) or raises ValueError with a message safe for the model.
    plan_paths: lower-case planned paths (default: this project's saved plan), used to tell a wrapper folder from a real one."""
    parts = _norm_parts(path)
    root = _root(project)
    plan = plan_paths if plan_paths is not None else {f["path"].lower() for f in _load_plan(project)}
    # "project/", "projects/" or the project's own name in front of the path is a wrapper the model sometimes adds, not a folder: but
    # blog/index.html in project "blog" is a real page when the plan lists it, when it exists, or when index.html is already there.
    for _ in range(2):
        first = parts[0]
        if len(parts) > 1 and (first.lower() in ("project", "projects") or _slug(first) == _slug(project)):
            whole, rest = "/".join(parts), "/".join(parts[1:])
            if whole.lower() in plan or (root / whole).exists():
                break
            if (root / rest).is_file():
                raise ValueError(f"error: `{whole}` is unclear: `{rest}` already exists in this project. Use `{rest}` for the project's "
                                 f"own file, or list `{whole}` in project_plan if it is a different page in a folder.")
            parts = parts[1:]
        else:
            break
    for p in parts:
        _check_name(p)
    for p in parts[:-1]:
        if p.lower().endswith(TEXT_EXTS):
            raise ValueError(f"error: `{p}` looks like a file name: a folder name must not end with {', '.join(TEXT_EXTS[:4])} ...")
    rel = "/".join(parts)
    if not rel.lower().endswith(TEXT_EXTS):
        raise ValueError(_BAD_EXT)
    if _pan_text(rel) or _pan_text(str(project or "")[:200]):
        raise ValueError("error: write refused: a file or project name looks like a payment card number (PCI DSS). Rename it.")
    base = root.resolve()
    cur = root
    for p in parts[:-1]:
        other = _same_name_other_case(cur, p)
        cur = cur / p
        if other or cur.is_symlink():
            raise ValueError("error: path conflicts with an existing folder (names are case-insensitive, no links allowed)")
        if cur.is_file():
            raise ValueError(f"error: `{p}` is already a file in this project, so it cannot also be a folder")
    target = cur / parts[-1]
    other = _same_name_other_case(cur, parts[-1])
    if other:
        raise ValueError(f"error: `{other}` already exists: file names are case-insensitive, use that exact name")
    if target.is_symlink():
        raise ValueError("error: path conflicts with an existing link; use another file name")
    if target.is_dir():
        raise ValueError(f"error: `{rel}` is a folder in this project, not a file")
    target = target.resolve()
    if base not in target.parents:
        raise ValueError("error: path escapes the project folder")
    return root, target, rel


_BACKUP_SUFFIX = ".prev"
_TMP_SUFFIX = ".tmp"
_RECORD_NAME = ".finished.json"


def _project_files(root: Path) -> list:
    """The files of a project, without the one-step `.prev` backups a replacement leaves next to a file, temp files, the finish
    record, and anything that is a link or resolves outside the folder."""
    if not root.is_dir():
        return []
    base = root.resolve()
    out = []
    for f in root.rglob("*"):
        if f.suffix in (_BACKUP_SUFFIX, _TMP_SUFFIX) or f.name == _RECORD_NAME or f.is_symlink() or not f.is_file():
            continue
        if base in f.resolve().parents:
            out.append(f)
    return out


def _size(root: Path) -> tuple:
    files = _project_files(root)
    return len(files), sum(f.stat().st_size for f in files)


def _total_bytes(root: Path) -> int:
    """Everything the project takes on disk, the `.prev` backups included (they count against the limit)."""
    total = 0
    for f in root.rglob("*") if root.is_dir() else []:
        if f.suffix != _TMP_SUFFIX and f.name != _RECORD_NAME and f.is_file() and not f.is_symlink():
            total += f.stat().st_size
    return total


_CODE_EXTS = (".html", ".htm", ".css", ".js", ".mjs", ".ts", ".json", ".svg", ".xml", ".webmanifest", ".py")
_MARKUP_EXTS = (".html", ".htm", ".svg")          # code, but also prose the reader sees (text between the tags)
_TAG_RX = re.compile(r"<[^<>]{0,500}>")


def _pan_text(text: str) -> bool:
    """A card number in a short label (file name, project name): the strict check only."""
    from .. import pan
    return bool(text) and pan.enabled("pan_input") and pan.contains_pan(text, contextual=False)


def _first_pan_line(text: str, contextual: bool):
    """Line number (1-based) of the first card-like number or CVV in `text`, or None. Never returns the digits."""
    from .. import pan
    try:
        for m in pan.PAN_RX.finditer(text):
            sp = pan._spans(m.group(0), text, m.start(), contextual)
            if sp:
                return text.count("\n", 0, m.start() + sp[0][0]) + 1
        m = pan.CVV_RX.search(text)
        if m and (contextual or re.match(r"\w+", m.group(0)).group(0).lower() not in ("csc", "cid")):
            return text.count("\n", 0, m.start()) + 1
    except Exception:
        pass
    return None


def _text_variants(text: str, rel: str) -> list:
    """The text as a reader sees it, for the contextual card check: markup removed (once to nothing, once to a space, so a number split
    over <span>s or <br>s is whole again) and entities decoded (&nbsp;, &#52;). Script, style and comments are left out."""
    low = rel.lower()
    if not low.endswith(_MARKUP_EXTS) and "<" not in text and "&" not in text:
        return []
    if low.endswith(_MARKUP_EXTS):
        spans, _ = _raw_spans(text)
        text = _cut_spans(text, spans, ("script", "style", "comment"))
    elif low.endswith(_CODE_EXTS):
        return []
    return [_htmllib.unescape(_TAG_RX.sub(r, text)) for r in ("", " ")]


def _pan_check(content: str, rel: str = "", before: str = ""):
    """A card number is refused on every write. Code files get the strict check on the file as written: a number that is a card number
    by itself (Luhn, issuer prefix, standalone). The "card / cvv / expiry within a few words" loosening flags harmless WebGL / seed / hex
    constants next to words like cardinal, so it is not used for code, but it IS used for the words a page shows (text between the tags,
    entities decoded). `before`: the end of the file this text is appended to, so a number split over two appends is still seen.
    The message names the LINE only (never the digits) so a false positive can be fixed instead of retried."""
    from .. import pan
    low = rel.lower()
    on = pan.enabled("pan_input")
    scanned = (before[-40:] + "\n" + content) if before else content
    skew = scanned.count("\n", 0, len(scanned) - len(content)) if before else 0
    if low.endswith(_CODE_EXTS):
        if on:
            name = rel or "this file"
            if pan.contains_pan(scanned, contextual=False):
                where = f"line {max(1, (_first_pan_line(scanned, False) or 1 + skew) - skew)} of {name}, counted in this piece"
            elif any(pan.contains_pan(v) for v in _text_variants(scanned, rel)):
                where = f"in the text the page shows in {name}: check numbers between tags or written with entities"
            else:
                return None
            # Strict on purpose: a Luhn-valid 13-19 digit constant is refused even when it is only a seed or id (a missed card number
            # is a PCI breach, a false alarm costs one retry). The advice names the two ways out and no way to hide a real number.
            return (f"error: write refused: this looks like a payment card number ({where}). If it is not one, do not use a 13-19 "
                    "digit literal: use a shorter constant (under 13 digits) or generate the value in code (for example from "
                    "Math.random() / Date.now()), then write the file again. If it is a real card number, remove it "
                    "(PCI DSS: card numbers must not be written into documents).")
        return None
    if on and pan.contains_pan(scanned):       # the text itself (doc_ops' check runs on a JSON copy, where a newline hides a split number)
        line = max(1, (_first_pan_line(scanned, True) or 1 + skew) - skew)
        return (f"error: write refused: line {line} of this piece contains a payment card number; card numbers must not be written "
                "into documents (PCI DSS). Use a masked value such as ****1234.")
    if on and any(pan.contains_pan(v) for v in _text_variants(scanned, rel)):
        return ("error: write refused: the text contains a payment card number written with markup or entities; card numbers must not "
                "be written into documents (PCI DSS). Remove it or use a masked value such as ****1234.")
    return None


# ---- the plan: the file list the model declares first; "done" is read from disk, never remembered ------------------------------

def _plan_path(project) -> Path:
    return _common_resolve(f"{PROJECTS_DIR}/{_scope()}/{_slug(project)}.plan.json")


def _load_plan(project) -> list:
    try:
        data = json.loads(_plan_path(project).read_text(encoding="utf-8"))
        return [f for f in data.get("files", []) if isinstance(f, dict) and f.get("path")]
    except Exception:
        return []


def _lines(f: Path) -> int:
    try:
        return len(f.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return 0


def plan_status(project) -> dict:
    """{'plan': [...], 'done': [...], 'todo': [...]} for a project: a planned file is done once it exists with its min_lines."""
    plan = _load_plan(project)
    root = _root(project)
    done, todo = [], []
    for f in plan:
        target = root / f["path"]
        need = max(1, int(f.get("min_lines") or 5))
        (done if target.is_file() and _lines(target) >= need else todo).append(f)
    return {"plan": plan, "done": done, "todo": todo}


def _progress(project) -> str:
    st = plan_status(project)
    if not st["plan"]:
        return ""
    if not st["todo"]:
        return f" Plan: all {len(st['plan'])} files done - call project_finish."
    nxt = st["todo"][0]
    return (f" Plan: {len(st['done'])} of {len(st['plan'])} files done. Next: {nxt['path']} ({nxt.get('purpose', '')}); "
            f"still missing: {', '.join(f['path'] for f in st['todo'][:8])}.")


def tool_project_plan(args: dict) -> str:
    a = args or {}
    files = a.get("files")
    if isinstance(files, str):
        try:
            files = json.loads(files)
        except ValueError:
            files = None
    if not isinstance(files, list) or not files:
        return "error: send the plan as a list of files, each with a path and a one-line purpose."
    items = []
    for item in files:
        if isinstance(item, str):
            item = {"path": item, "purpose": ""}
        if isinstance(item, dict):
            items.append(item)
    try:
        raw = {"/".join(_norm_parts(i.get("path"))).lower() for i in items}      # lets blog/index.html stay a page of its own
        clean, seen = [], {}
        for item in items:
            _, _, rel = _file(a.get("project"), item.get("path"), plan_paths=raw)
            rawp = "/".join(_norm_parts(item.get("path")))
            if rel.lower() in seen:
                if seen[rel.lower()] != rawp:
                    return (f"error: the plan paths `{seen[rel.lower()]}` and `{rawp}` would be the same file `{rel}` "
                            "(names are case-insensitive and a leading project folder is dropped). Give them different names.")
                continue
            seen[rel.lower()] = rawp
            try:
                need = max(1, min(400, int(item.get("min_lines") or 5)))
            except (TypeError, ValueError):
                need = 5
            purpose = str(item.get("purpose") or "")[:140]
            from .. import pan
            if pan.enabled("pan_input") and pan.contains_pan(purpose):
                return "error: write refused: a purpose line contains a payment card number (PCI DSS). Describe the file without it."
            clean.append({"path": rel, "purpose": purpose, "min_lines": need})
    except ValueError as e:
        return str(e)
    if len(clean) > MAX_PLAN_FILES:
        return f"error: plan at most {MAX_PLAN_FILES} files; merge small ones (one css file, one or two js files)."
    if not any(f["path"].lower().endswith((".html", ".htm")) for f in clean):
        return "error: the plan has no HTML page. Add index.html."
    try:
        with _lock(a.get("project")):
            pp = _plan_path(a.get("project"))
            pp.parent.mkdir(parents=True, exist_ok=True)
            _write_atomic(pp, json.dumps({"files": clean}, ensure_ascii=False))
    except (OSError, ValueError):
        return _STORAGE_ERROR
    return (f"Plan saved for project {_slug(a.get('project'))}: {len(clean)} files." + _progress(a.get("project"))
            + " Write them one at a time with project_write.")


_HTML_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr"
}
_HTML_INLINE_TAGS = {
    "a", "b", "strong", "i", "em", "span", "code", "small", "label",
    "button", "sub", "sup", "mark", "u", "abbr"
}
_HTML_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_HTML_SECTIONING_TAGS = {"section", "article", "aside", "nav", "header", "footer", "main"}
_HTML_BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "canvas", "dd", "div",
    "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form",
    "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main",
    "nav", "noscript", "ol", "p", "pre", "section", "table", "tfoot",
    "ul", "video"
}
# A start tag of these closes an open <p> by itself (the browser does it too)
_CLOSES_P = _HTML_BLOCK_TAGS | {"details", "menu", "summary"}


# ---- raw-text spans: comments, <script>, <style>, <textarea>, <title> hold text that is not markup ------------------------------
# One linear pass finds them, so `</body>` or `<style>` written inside a script string, a comment or a CSS value is never taken for
# a tag. (The old regexes backtracked: a script with `i<n && total < 1e9` hung the server.)
_RAW_OPEN = re.compile(r"<!--|<(script|style|textarea|title)(?=[\s/>])", re.I)
_RAW_CLOSE = {k: re.compile(rf"</{k}(?=[\s/>])", re.I) for k in ("script", "style", "textarea", "title")}
_BODY_END = re.compile(r"</body\b[^>]*>", re.I)
_HTML_END = re.compile(r"</html\b[^>]*>", re.I)
_HTML_START = re.compile(r"<html(?=[\s/>])", re.I)


def _raw_spans(text: str) -> tuple:
    """([(start, end, kind, closed)], kind of an unclosed last span or None); kind is comment / script / style / textarea / title."""
    spans, pos, n = [], 0, len(text)
    while True:
        m = _RAW_OPEN.search(text, pos)
        if not m:
            return spans, None
        if m.group(0) == "<!--":
            e = text.find("-->", m.end())
            if e < 0:
                spans.append((m.start(), n, "comment", False))
                return spans, "comment"
            spans.append((m.start(), e + 3, "comment", True))
            pos = e + 3
            continue
        kind = m.group(1).lower()
        gt = text.find(">", m.end())
        cm = _RAW_CLOSE[kind].search(text, gt + 1) if gt >= 0 else None
        if not cm:
            spans.append((m.start(), n, kind, False))
            return spans, kind
        end = text.find(">", cm.end())
        end = n if end < 0 else end + 1
        spans.append((m.start(), end, kind, True))
        pos = end


def _in_spans(spans: list, pos: int) -> bool:
    i = bisect.bisect_right(spans, (pos, float("inf"))) - 1
    return i >= 0 and spans[i][0] <= pos < spans[i][1]


def _find_outside(text: str, rx, spans: list, last: bool = False):
    """First (or last) match of `rx` that does not start inside a raw-text span."""
    found = None
    for m in rx.finditer(text):
        if not _in_spans(spans, m.start()):
            if not last:
                return m
            found = m
    return found


def _cut_spans(text: str, spans: list, kinds: tuple) -> str:
    out, pos = [], 0
    for s0, e0, kind, _ in spans:
        if kind in kinds:
            out.append(text[pos:s0])
            out.append(" ")
            pos = e0
    out.append(text[pos:])
    return "".join(out)


class _TagStackParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []

    def _pop_to(self, names, stop):
        """An open <li>/<td>/<option>... that this start tag ends implicitly (not past `stop`, the list / table that owns it)."""
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i] in stop:
                return
            if self.stack[i] in names:
                self.stack = self.stack[:i]
                return

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        # end tags the HTML syntax lets a page leave out: the browser closes them, so a closing tag appended later would be stray
        if t in _CLOSES_P and self.stack and self.stack[-1] == "p":
            self.stack.pop()
        if t == "li":
            self._pop_to({"li"}, {"ul", "ol", "menu"})
        elif t in ("dd", "dt"):
            self._pop_to({"dd", "dt"}, {"dl"})
        elif t in ("td", "th"):
            self._pop_to({"td", "th"}, {"tr", "table"})
        elif t == "tr":
            self._pop_to({"td", "th", "tr"}, {"table", "tbody", "thead", "tfoot"})
        elif t in ("tbody", "thead", "tfoot"):
            self._pop_to({"td", "th", "tr", "tbody", "thead", "tfoot"}, {"table"})
        elif t == "option":
            self._pop_to({"option"}, {"select", "datalist", "optgroup"})
        if t not in _HTML_VOID_TAGS:
            self.stack.append(t)

    def handle_endtag(self, tag):
        t = tag.lower()
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i] == t:
                self.stack = self.stack[:i]
                break

    def handle_startendtag(self, tag, attrs):
        pass


def _get_open_tags(html_str: str) -> list[str]:
    spans, _ = _raw_spans(html_str)
    cleaned = _cut_spans(html_str, spans, ("script", "style", "comment", "textarea", "title"))
    parser = _TagStackParser()
    try:
        parser.feed(cleaned)
    except Exception:
        pass
    return parser.stack


def _find_first_html_tag(snippet: str):
    """Returns (is_closing: bool, tag_name: str) or None."""
    s = snippet.lstrip()
    while s.startswith("<!--"):                       # leading comments are not the first tag
        e = s.find("-->")
        if e < 0:
            return None
        s = s[e + 3:].lstrip()
    m = re.match(r"<(/)?([a-zA-Z0-9:-]+)", s)
    if m:
        return (bool(m.group(1)), m.group(2).lower())
    return None


def _calculate_auto_closing_tags(open_stack: list[str], incoming_snippet: str, tail_text=None) -> list[str]:
    """Tags to close before incoming_snippet is appended. Only what the cut-off chunk left unterminated in the middle of its text:
    a trailing run of inline tags, headings and <p> (`<p>Some text <em>emphasis` + `<h3>`). A wrapper (main, div, section, article,
    nav, ul, ol, table, an <a class="card"> that holds blocks) is never closed on a guess: the next chunk is usually its content.
    tail_text: the text after the last tag of the old chunk; empty means the chunk ended cleanly on a tag, so nothing is closed."""
    usable_stack = [t for t in open_stack if t not in ("html", "body")]
    if not usable_stack:
        return []
    if tail_text is not None and not tail_text.strip():
        return []
    first_tag_info = _find_first_html_tag(incoming_snippet)
    if not first_tag_info or first_tag_info[0]:
        return []
    tag = first_tag_info[1]
    if not (tag in _HTML_BLOCK_TAGS or tag in _HTML_HEADING_TAGS or tag == "p"):
        return []
    curr_stack = list(usable_stack)
    tags_to_close = []
    while curr_stack and (curr_stack[-1] in _HTML_INLINE_TAGS or curr_stack[-1] in _HTML_HEADING_TAGS or curr_stack[-1] == "p"):
        tags_to_close.append(curr_stack.pop())
    if not tags_to_close:
        return []
    # a new section after a section that was cut off mid-text: the cut-off section ends here too (never main: it holds all sections)
    if tag in _HTML_SECTIONING_TAGS:
        for i in range(len(curr_stack) - 1, -1, -1):
            if curr_stack[i] in _HTML_SECTIONING_TAGS and curr_stack[i] != "main":
                tags_to_close.extend(reversed(curr_stack[i:]))
                break
    return tags_to_close


def _trailing_script_start(before: str, spans: list):
    """Start of a short <script> block that ends `before` (a page's own script sits last in the body), else None."""
    end = len(before.rstrip())
    if spans and spans[-1][2] == "script" and spans[-1][3] and spans[-1][1] == end and end - spans[-1][0] < 3000:
        return spans[-1][0]
    return None


def _balance_html_document(html_str: str) -> str:
    """Ensure all open tags inside <body> are closed before </body> or </html>."""
    spans, _ = _raw_spans(html_str)
    m_body = _find_outside(html_str, _BODY_END, spans)
    if not m_body:
        return html_str
    idx = m_body.start()
    before = html_str[:idx]
    after = html_str[idx:]

    s_idx = _trailing_script_start(before, [s for s in spans if s[1] <= idx])
    trailing_script = ""
    if s_idx is not None:
        trailing_script = before[s_idx:]
        before = before[:s_idx]

    open_tags = _get_open_tags(before)
    dangling = [t for t in open_tags if t not in ("html", "body")]
    if dangling:
        close_html = "\n" + "\n".join(f"</{t}>" for t in reversed(dangling))
        before = before.rstrip() + close_html + "\n"

    return before + trailing_script + after


def _close_document(html_str: str) -> str:
    """A page cut off mid-way (no </body> / </html>, an open <script>) still downloads as a working page: close what is open.
    A page that is already whole (<html ... </html>, every script / style / comment closed) is returned exactly as written."""
    if not re.search(r"<html\b|<body\b|<!doctype", html_str, re.I):
        return html_str                       # a fragment, not a page: leave it as written
    spans, unclosed = _raw_spans(html_str)
    if (not unclosed and _find_outside(html_str, _HTML_START, spans) and _find_outside(html_str, _HTML_END, spans)):
        return html_str
    out = html_str
    if unclosed:
        out = out.rstrip() + ("\n-->\n" if unclosed == "comment" else f"\n</{unclosed}>\n")
        spans, _ = _raw_spans(out)
    if not _find_outside(out, _BODY_END, spans):
        m_html = _find_outside(out, _HTML_END, spans)
        head = out[:m_html.start()] if m_html else out
        dangling = [t for t in _get_open_tags(head) if t not in ("html", "body")]
        closing = "".join(f"\n</{t}>" for t in reversed(dangling)) + "\n</body>\n"
        out = head.rstrip() + closing + (out[m_html.start():] if m_html else "")
        spans, _ = _raw_spans(out)
    if not _find_outside(out, _HTML_END, spans):
        out = out.rstrip() + "\n</html>\n"
    return _balance_html_document(out)


def _smart_html_append(old: str, new_content: str) -> str:
    """Intelligently stitch an HTML fragment into an existing HTML document.

    Closes what the previous chunk left unterminated in the middle of its text (a cut-off <p>, <em>, link) when the appended chunk
    starts a new block, preventing broken DOM trees. Wrappers (main / div / section ...) are left open for the next chunk's content.
    """
    new_str = str(new_content or "").strip()
    if not new_str:
        return old

    # 1. Full document update: if new_content starts with DOCTYPE or <html>, replace entirely
    if re.search(r"^\s*<!DOCTYPE\s+html", new_str, re.I) or re.search(r"^\s*<html\b", new_str, re.I):
        return new_str

    # 2. Strip accidental trailing </body> or </html> from the appended fragment
    cleaned = re.sub(r"</(?:body|html)>\s*$", "", new_str, flags=re.I).strip()
    cleaned = re.sub(r"</(?:body|html)>\s*$", "", cleaned, flags=re.I).strip()

    # 3. Locate injection boundaries (a </body> inside a script string or a comment is not the end of the page)
    spans, unclosed = _raw_spans(old)
    if unclosed:                                  # the old text stops inside a script / style / comment: the new text continues it
        return old.rstrip() + "\n" + cleaned + "\n"
    before_body, after_body = old, ""
    m_body = _find_outside(old, _BODY_END, spans) or _find_outside(old, _HTML_END, spans)
    if m_body:
        idx = m_body.start()
        before_body = old[:idx]
        after_body = old[idx:]
        if m_body.group(0)[:6].lower() == "</body":
            s_idx = _trailing_script_start(before_body, [s for s in spans if s[1] <= idx])
            if s_idx is not None:
                after_body = before_body[s_idx:] + after_body
                before_body = before_body[:s_idx]

    # 4. Fix dangling unclosed markdown link at the end of before_body (e.g. `[2](https://...`); only the last 4 KB is looked at
    b_trimmed = before_body.rstrip()
    if re.search(r"\[[^\]\n]{1,200}\]\((?:https?://)?[^\s\)]*$", b_trimmed[-4096:]):
        before_body = b_trimmed + ")"

    # 5. Fix a dangling incomplete opening tag (a cut-off `<div class="` at the end): the last "<" after the last ">", close to the
    #    end, on one line, followed by a tag name. `score <5` is text and stays.
    b_trimmed = before_body.rstrip()
    lt, gt = b_trimmed.rfind("<"), b_trimmed.rfind(">")
    if (lt > gt and len(b_trimmed) - lt < 200 and "\n" not in b_trimmed[lt:] and re.match(r"</?[A-Za-z][\w:-]*", b_trimmed[lt:lt + 40])
            and not _in_spans(_raw_spans(b_trimmed)[0], lt)):
        before_body = b_trimmed[:lt]

    # 6. Close what the previous chunk left unterminated mid-text if the new chunk starts a block
    b_trimmed = before_body.rstrip()
    tail_text = b_trimmed[b_trimmed.rfind(">") + 1:][-4096:]
    open_tags = _get_open_tags(before_body)
    to_close = _calculate_auto_closing_tags(open_tags, cleaned, tail_text)
    if to_close:
        close_html = "\n".join(f"</{t}>" for t in to_close)
        before_body = before_body.rstrip() + "\n" + close_html

    # 7. Assemble
    if after_body:
        return before_body.rstrip() + "\n\n" + cleaned + "\n\n" + after_body.lstrip()
    return before_body.rstrip() + ("\n\n" if not before_body.endswith("\n\n") else "") + cleaned + "\n"


_FENCE_RX = re.compile(r"^\s*```[\w+-]*[ \t]*\n([\s\S]*?)\n?```\s*$")
_DOC_START_RX = re.compile(r"<!doctype\s+html|<html\b", re.I)


def _is_html(rel: str) -> bool:
    return rel.lower().endswith((".html", ".htm"))


def _clean_content(text: str, rel: str) -> str:
    """Models wrap a file in ``` fences or put a sentence in front of it. Neither belongs in the file: strip a fence around the
    whole text and, for a page, everything before <!doctype / <html and after the last </html>. Plain text comes back untouched."""
    out = text
    if not rel.lower().endswith((".md", ".txt")):
        m = _FENCE_RX.match(out)
        if m:
            out = m.group(1)
    if _is_html(rel):
        m = _DOC_START_RX.search(out)
        if m and m.start() > 0 and "</html>" in out.lower():
            out = out[m.start():]
        end = out.lower().rfind("</html>")
        if end != -1 and _DOC_START_RX.search(out[:end]):
            out = out[:end + len("</html>")]
    return text if out == text or not out.strip() else out.strip("\n") + "\n"


def _is_full_html(text: str) -> bool:
    low = text.lower()
    return bool(_DOC_START_RX.search(low)) and "</html>" in low


def _is_complete_replacement(rel: str, new: str, old: str) -> bool:
    """Is `new` a whole new version of the file (so it may replace `old`) rather than a fragment or a cut-off rewrite?
    A page: it has <html ...> and </html>. Anything else has no cheap check: it must be about as long as the old file (60%), or the
    old file is still short (under 10 lines), i.e. a skeleton."""
    if _is_html(rel):
        return _is_full_html(new)
    return len(new) >= 0.6 * len(old) or len(old.splitlines()) < 10


def _hash(text: str) -> str:
    import hashlib
    return hashlib.sha1(text.strip().encode("utf-8", "replace")).hexdigest()


def _store(args: dict, append: bool) -> str:
    a = args or {}
    content = a.get("content")
    if content is None or content == "":
        content = next((a[k] for k in ("text", "html", "code", "body") if isinstance(a.get(k), str) and a[k].strip()), "")
    if not str(content).strip():
        return "error: the 'content' argument was empty. Send the text of this file in 'content'."
    if len(str(content).encode("utf-8")) > MAX_EDIT_BYTES:
        return "error: this piece is too large: send under about 150 lines per call and use project_append for the rest."
    try:
        root, target, rel = _file(a.get("project"), a.get("path") or a.get("file") or a.get("filename"))
    except ValueError as e:
        return str(e)
    text = _clean_content(str(content), rel)
    try:
        with _lock(a.get("project")):
            return _store_locked(a, append, root, target, rel, text)
    except (OSError, ValueError):
        return _STORAGE_ERROR


def _store_locked(a: dict, append: bool, root: Path, target: Path, rel: str, text: str) -> str:
    existed = target.is_file()
    old = target.read_text(encoding="utf-8", errors="replace") if existed else ""
    refused = _pan_check(text, rel, old if append and existed else "")
    if refused:
        return refused
    if not _load_plan(a.get("project")) and not (root.is_dir() and any(root.rglob("*"))):
        return ("error: plan the site first: call project_plan with the complete list of files (at most 14, each with a one-line "
                "purpose), then write them one at a time.")
    nxt = _progress(a.get("project")).strip() or "Call project_finish when all planned files are written."
    # An append of a whole page is a rewrite, and an append to a file that is not there yet is a write.
    if append and not existed:
        append = False
    elif append and _is_html(rel) and (_is_full_html(text) or _DOC_START_RX.match(text.lstrip())):
        append, a = False, {**a, "replace": True}
    replaced = False
    if existed and not append:
        if old.strip() == text.strip():
            return (f"note: {rel} already has this exact content ({_lines(target)} lines): nothing to redo. "
                    f"Call project_finish if every file is written, or project_append to add a new section. {nxt}")
        if not a.get("replace"):
            if _is_complete_replacement(rel, text, old):
                replaced = True               # a whole new version after a partial / failed first try: that is a redo, not a repeat
            else:
                done = {f["path"] for f in plan_status(a.get("project"))["done"]}
                plan = _load_plan(a.get("project"))
                if rel in done or not plan or _lines(target) >= 10:
                    return (f"error: {rel} is already written ({_lines(target)} lines) and this text is not a complete version of it. "
                            f"Use project_append to add a section, send the whole file to replace it, or pass replace=true to redo "
                            f"it from scratch; call project_finish if it is complete. {nxt}")
        else:
            replaced = True
    count, total = _size(root)
    if not existed and count >= MAX_FILES:
        return f"error: a project holds at most {MAX_FILES} files."
    if append:                                 # the size is checked on what will be stored, after an html merge added its closing tags
        if _is_html(rel):
            text = _smart_html_append(old, text)
        else:
            text = old + ("" if old.endswith("\n") else "\n") + text
    prev = target.with_name(target.name + _BACKUP_SUFFIX)
    on_disk = _total_bytes(root)                # the one-step backups count too
    cur_prev = prev.stat().st_size if prev.is_file() else 0
    new_b, old_b = len(text.encode("utf-8")), len(old.encode("utf-8"))
    projected = on_disk - old_b + new_b if append else on_disk - old_b - cur_prev + new_b + old_b
    if projected > MAX_PROJECT_BYTES:
        return (f"error: the project would be {projected // 1024} KB, over its limit of {MAX_PROJECT_BYTES // 1024} KB (backups of "
                "replaced files count). Shorten the content or remove what is not needed.")
    target.parent.mkdir(parents=True, exist_ok=True)
    if not append and existed:
        try:                                   # one undo step next to the file; never listed or zipped (see _project_files)
            _write_atomic(prev, old)
        except OSError:
            pass
    _write_atomic(target, text)
    if not (root / _RECORD_NAME).exists():     # marks a project made since the record exists: "never finished" (see _finished)
        _write_atomic(root / _RECORD_NAME, json.dumps({"sig": None, "complete": False, "history": []}))
    verb = "Appended to" if append else ("Replaced" if existed else "Created")
    unplanned = ""
    plan = _load_plan(a.get("project"))
    if plan and rel not in {f["path"] for f in plan}:
        unplanned = " (this file is not in the plan: fine, or add it with project_plan)"
    again = " - replaced the earlier version (one backup step kept)" if replaced else ""
    return (f"{verb} {rel} in project {_slug(a.get('project'))} ({len(text.splitlines())} lines, {len(text)} chars){again}{unplanned}."
            + (_progress(a.get("project")) or " Continue with the next piece, or call project_finish when every file is written."))


def tool_project_write(args: dict) -> str:
    return _store(args, append=False)


def tool_project_append(args: dict) -> str:
    return _store(args, append=True)


_LINK_CSS = re.compile(r"<link\b[^>]*\brel=[\"']stylesheet[\"'][^>]*>", re.I)
_SCRIPT_SRC = re.compile(r"<script\b([^>]*)\bsrc=[\"']([^\"']+)[\"']([^>]*)>\s*</script>", re.I)
_HREF = re.compile(r"\bhref=[\"']([^\"']+)[\"']", re.I)
_ATTR_DEFER = re.compile(r"""("[^"]*"|'[^']*')|(?<![\w-])(?:defer|async)(?![\w-])(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+))?""", re.I)


def _ref_rel(ref: str):
    """The project-relative path a page reference names, or None for anything that is not a plain relative file of the project:
    an address (https:, data:, //host), an anchor, an absolute path, or a path that climbs out of the folder (../)."""
    from urllib.parse import unquote
    ref = ref.strip()
    if not ref or re.match(r"^(?:[a-z][a-z0-9+.-]*:|//|#|/|\\)", ref, re.I):
        return None
    path = unquote(ref.split("?")[0].split("#")[0]).replace("\\", "/")
    parts = [x for x in path.split("/") if x not in ("", ".")]
    if not parts or ".." in parts or "\x00" in path or re.match(r"^[A-Za-z]:", parts[0]):
        return None
    return "/".join(parts)


def _local(root: Path, ref: str):
    """The file inside the project that `ref` names (a regular text file, no link, never outside the folder), else None."""
    rel = _ref_rel(ref)
    if not rel or not rel.lower().endswith(TEXT_EXTS):
        return None
    try:
        raw = root / rel
        p = raw.resolve()
        if raw.is_symlink() or not p.is_file() or root.resolve() not in p.parents:
            return None
        if any((root / Path(*rel.split("/")[:i])).is_symlink() for i in range(1, len(rel.split("/")))):
            return None
        return p
    except (OSError, ValueError):
        return None


def _entry_file(root: Path, entry: str) -> Path:
    """The entry page, read only if it is a regular file inside the project folder."""
    p = (root / entry)
    rp = p.resolve()
    if p.is_symlink() or root.resolve() not in rp.parents or not rp.is_file():
        raise ValueError("error: the entry page must be a file inside the project")
    return rp


def inline_entry(root: Path, entry: str) -> str:
    """The entry page with its local stylesheet and script files written into it, so one file previews like the site."""
    html = _entry_file(root, entry).read_text(encoding="utf-8", errors="replace")
    html = _close_document(html)
    later = []                          # <script src defer|async> must run after the body exists: they go to the end of <body>

    def css(m):
        h = _HREF.search(m.group(0))
        p = _local(root, h.group(1)) if h else None
        if not p:
            return m.group(0)
        return "<style>\n" + re.sub(r"</(style)", r"<\\/\1", p.read_text(encoding="utf-8", errors="replace"), flags=re.I) + "\n</style>"

    def js(m):
        p = _local(root, m.group(2))
        if not p:
            return m.group(0)
        body = re.sub(r"</(script)", r"<\\/\1", p.read_text(encoding="utf-8", errors="replace"), flags=re.I)
        attrs = m.group(1) + m.group(3)
        deferred = any(not x.group(1) for x in _ATTR_DEFER.finditer(attrs))
        is_module = re.search(r"\btype\s*=\s*[\"']?module\b", attrs, re.I)
        if deferred and not is_module:
            attrs = _ATTR_DEFER.sub(lambda x: x.group(1) or "", attrs)
            later.append(f"<script{attrs}>\n{body}\n</script>")
            return ""
        return f"<script{attrs}>\n{body}\n</script>"

    spans, _ = _raw_spans(html)         # only real tags are replaced: not a <link> or <script src> written inside a script string
    out, pos = [], 0
    for s0, e0, kind, _ in spans:
        out.append(_LINK_CSS.sub(css, html[pos:s0]))
        seg = html[s0:e0]
        m = _SCRIPT_SRC.fullmatch(seg) if kind == "script" else None
        out.append(js(m) if m else seg)
        pos = e0
    out.append(_LINK_CSS.sub(css, html[pos:]))
    html = "".join(out)
    if later:
        spans, _ = _raw_spans(html)
        m_end = _find_outside(html, _BODY_END, spans, last=True) or _find_outside(html, _HTML_END, spans, last=True)
        block = "\n".join(later) + "\n"
        html = html[:m_end.start()] + block + html[m_end.start():] if m_end else html.rstrip() + "\n" + block
    return html


def _missing_links(root: Path, entry: str) -> list:
    html = _entry_file(root, entry).read_text(encoding="utf-8", errors="replace")
    refs = [m.group(1) for m in _HREF.finditer(html)] + re.findall(r"\bsrc=[\"']([^\"']+)[\"']", html, re.I)
    out = set()
    for r in refs:
        rel = _ref_rel(r)                       # addresses, absolute paths and paths that leave the folder are not project files
        if rel and rel.lower().endswith(TEXT_EXTS) and _local(root, r) is None:
            out.add(r)
    return sorted(out)


def tool_project_finish(args: dict) -> str:
    return _finish(args or {}, force=False)


_NOT_FINISHED = "(not finished:"


def finish_result_ok(text) -> bool:
    """True when a project_finish / finish_if_possible result is a real, complete delivery: not an error, and not marked
    "(not finished: ...)" (planned files missing, or the page links to files that do not exist)."""
    t = str(text or "")
    return bool(t) and not t.startswith("error") and _NOT_FINISHED not in t


def _signature(root: Path) -> list:
    """[files, bytes, newest mtime in ns] of the source files: equal after a finish means nothing was edited since."""
    files = _project_files(root)
    return [len(files), sum(f.stat().st_size for f in files), max((f.stat().st_mtime_ns for f in files), default=0)]


def _read_record(root: Path) -> dict:
    try:
        rec = json.loads((root / _RECORD_NAME).read_text(encoding="utf-8"))
        return rec if isinstance(rec, dict) else {}
    except (OSError, ValueError):
        return {}


def _finished(root: Path, need_complete: bool = False) -> bool:
    """Has this project been packaged since its files last changed? The record project_finish leaves in the project folder says so
    (zip, page, time and the files' signature at that moment); it belongs to this project of this chat only. need_complete: a package
    made with planned files missing does not count (a resumed "continue" must still finish the job). Projects finished before the
    record existed: a `<slug>-<8 hex>.zip|html` in the common folder at least as new as the newest source file."""
    rec = _read_record(root)
    if rec:
        return rec.get("sig") == _signature(root) and (bool(rec.get("complete")) or not need_complete)
    files = _project_files(root)
    if not files:
        return False
    newest = max(f.stat().st_mtime for f in files)
    made = re.compile(rf"^{re.escape(root.name)}-[0-9a-f]{{8}}\.(?:zip|html)$")
    return any(made.match(f.name) and f.stat().st_mtime >= newest for f in common_workspace().iterdir() if f.is_file())


def _save_record(root: Path, slug: str, zip_name, page_name, complete: bool, common: Path) -> None:
    """Write the finish record and keep only the newest 3 deliverable sets (zip + page) of this project in the common folder."""
    old = _read_record(root).get("history") or []
    hist = [h for h in old if isinstance(h, dict)] + [{"zip": zip_name, "page": page_name}]
    keep, drop = hist[-3:], hist[:-3]
    import time
    _write_atomic(root / _RECORD_NAME, json.dumps({"zip": zip_name, "page": page_name, "time": time.time(), "sig": _signature(root),
                                                   "complete": complete, "history": keep}))
    made = re.compile(rf"^{re.escape(slug)}-[0-9a-f]{{8}}\.(?:zip|html)$")
    for h in drop:
        for name in (h.get("zip"), h.get("page")):
            if isinstance(name, str) and made.match(name):
                try:
                    (common / name).unlink()
                except OSError:
                    pass


def _finish(a: dict, force: bool) -> str:
    """force=True (finish_if_possible): package whatever is written, even with planned files missing or no page at all."""
    try:
        with _lock(a.get("project")):
            return _finish_locked(a, force)
    except (OSError, ValueError):
        return _STORAGE_ERROR


def _finish_locked(a: dict, force: bool) -> str:
    root = _root(a.get("project"))
    files = sorted(_project_files(root))
    if not files:
        return "error: this project has no files yet. Create them with project_write first."
    st = plan_status(a.get("project"))
    unfinished = ""
    problems = []
    if st["todo"]:
        listing = "; ".join(f"{f['path']} ({f.get('purpose', '')})" for f in st["todo"][:10])
        if not force:
            return ("error: the plan is not finished: " + listing + ". Write these with project_write (project_append to lengthen a "
                    "short file), or drop one with project_plan, then call project_finish again.")
        unfinished = " NOTE: planned files still missing: " + listing + "."
        problems.append("planned files missing: " + ", ".join(f["path"] for f in st["todo"][:8]))
    entry = "index.html"
    if a.get("entry"):                          # same guard as a file write: inside the project, a page, no links
        try:
            _, _, entry = _file(a.get("project"), a.get("entry"))
        except ValueError as e:
            return str(e)
        if not _is_html(entry):
            return "error: the entry must be an html page of the project, like index.html."
    htmls = sorted(p.relative_to(root).as_posix() for p in files if p.suffix.lower() in (".html", ".htm"))
    if entry not in htmls:
        if not htmls and not force:
            return "error: the project has no HTML page. Create index.html with project_write."
        entry = ("index.html" if "index.html" in htmls else htmls[0]) if htmls else ""
    slug = _slug(a.get("project"))
    uid8 = uuid.uuid4().hex[:8]
    zip_name, page_name = f"{slug}-{uid8}.zip", f"{slug}-{uid8}.html"
    common = common_workspace()
    # Ensure all HTML files are properly closed (a page that is already whole is left exactly as written)
    for f in files:
        if f.suffix.lower() in (".html", ".htm"):
            try:
                raw_h = f.read_text(encoding="utf-8", errors="replace")
                bal_h = _close_document(raw_h)
                if bal_h != raw_h:
                    _write_atomic(f, bal_h)
            except OSError:
                pass
    single = len(files) == 1 and bool(entry)      # one html page: a ZIP of one file is noise, hand over just the page
    if not single:
        with zipfile.ZipFile(common / zip_name, "w", zipfile.ZIP_DEFLATED) as z:
            for f in files:
                z.write(f, f"{slug}/{f.relative_to(root).as_posix()}")
    note = ""
    if entry:
        from ..html_quality import repair as _repair_html
        page_html, typos = _repair_html(inline_entry(root, entry))      # near-miss class names and stray tags a small model leaves behind
        _write_atomic(common / page_name, page_html)
        note = f" (fixed {typos} typo(s) in the page's class names and tags automatically)" if typos else ""
        missing = _missing_links(root, entry)
        if missing:
            note = (" WARNING: the page links to files that do not exist yet: " + ", ".join(missing[:8])
                    + ". Create them with project_write and call project_finish again.")
            problems.append("the page links to files that do not exist: " + ", ".join(missing[:8]))
    empty = [f.relative_to(root).as_posix() for f in files if f.stat().st_size == 0]
    if empty:
        note += " WARNING: empty files: " + ", ".join(empty[:8]) + "."
    note += unfinished
    if problems:
        note += f" {_NOT_FINISHED} " + "; ".join(problems) + ")"
    _save_record(root, slug, None if single else zip_name, page_name if entry else None, not st["todo"], common)
    if single:
        return f"Page {slug}: 1 file. Download it as {page_name} [DOWNLOAD: {page_name}]. Reply now with a short summary and this link." + note
    if not entry:
        return (f"Project {slug}: {len(files)} files. Download them as {zip_name} [DOWNLOAD: {zip_name}]. Reply now with a short "
                f"summary and this link." + note)
    return (f"Project {slug}: {len(files)} files. Download the whole site as {zip_name} [DOWNLOAD: {zip_name}] and preview "
            f"{entry} as {page_name} [DOWNLOAD: {page_name}]. Reply now with a short summary and these two links." + note)


def finish_if_possible(project=None):
    """Safety net for the chat route: when a build ended without project_finish (model gave up, errored, pasted the page into chat),
    package what is on disk. Uses the current chat scope and user like the tools do (call it inside the same request).
    `project`: the project name of this turn (the route passes it), or None for the newest project of this chat that is not finished.
    Returns the project_finish text (with its [DOWNLOAD: ...] tag; check finish_result_ok) when that project has files that were
    changed since its last finish, else None. Never raises."""
    try:
        base = common_workspace() / PROJECTS_DIR / _scope()
        if not base.is_dir():
            return None
        best = None
        for d in base.iterdir():
            if not d.is_dir() or (project is not None and d.name != _slug(project)):
                continue
            files = _project_files(d)
            if not files or _finished(d):
                continue
            newest = max(f.stat().st_mtime for f in files)
            if best is None or newest > best[0]:
                best = (newest, d)
        if not best:
            return None
        out = _finish({"project": best[1].name}, force=True)
        return None if out.startswith("error") else out
    except Exception:
        return None


def open_project_note(max_age_s: int = 3 * 24 * 3600) -> str:
    """System note for a bare "continue" when a website project was left unfinished (stopped, restarted, cut off), else ''.
    A project is open until project_finish packaged it complete after its last edit (see _finished). Never raises."""
    import time
    try:
        base = common_workspace() / PROJECTS_DIR / _scope()
        if not base.is_dir():
            return ""
        best = None
        for d in base.iterdir():
            files = _project_files(d) if d.is_dir() else []
            if not files:
                continue
            newest = max(f.stat().st_mtime for f in files)
            if time.time() - newest > max_age_s or _finished(d, need_complete=True):
                continue
            if best is None or newest > best[0]:
                best = (newest, d, files)
        if not best:
            return ""
        newest, d, files = best
        listing = ", ".join(f"{f.relative_to(d).as_posix()} ({_lines(f)} lines)" for f in sorted(files)[:40])
        st = plan_status(d.name)
        if st["plan"]:
            plan_txt = (f" Plan: {len(st['done'])} of {len(st['plan'])} files done; still to write: "
                        + (", ".join(f"{f['path']} ({f.get('purpose', '')})" for f in st["todo"]) or "none - call project_finish") + ".")
        else:
            plan_txt = " It has no saved plan: call project_plan with the complete file list first."
        return (f"[system note] The user's website project `{d.name}` was interrupted before project_finish. Files so far: {listing}."
                f"{plan_txt} Continue it now by tool calls only (never paste code into the reply, use project=`{d.name}`): "
                "project_write the missing files, project_append to lengthen a file that is cut short, do not rewrite complete files, "
                "and call project_finish when the plan is done.")
    except Exception:
        return ""


def plan_items(project) -> list:
    """The plan as the chat's checklist shows it: [{ord, text, status}] with the first unfinished file marked in progress."""
    st = plan_status(project)
    todo = {f["path"] for f in st["todo"]}
    first = st["todo"][0]["path"] if st["todo"] else None
    return [{"ord": i + 1, "text": f["path"] + (f" - {f['purpose']}" if f.get("purpose") else ""),
             "status": "done" if f["path"] not in todo else ("in_progress" if f["path"] == first else "pending")}
            for i, f in enumerate(st["plan"])]
