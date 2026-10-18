"""Chat project builder: a multi-file static site (html / css / js / json / md / svg) written in pieces, then one ZIP.

A reply is cut off by the output limit long before a whole website fits in one write_file call, so the model builds it the way a
person would: project_write one file at a time, project_append the next section of a long file, project_finish to get the ZIP and
a single-file preview. Nothing here runs: files are inert text in the user's own common space (never another user's), the same
folder the other chat documents use. Card numbers are refused on every write, like every other document the chat saves."""
import re
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
        "content": {"type": "string", "description": "the complete text of this file (or of its skeleton)"}},
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

PROJECT_TOOL_NAMES = ("project_write", "project_append", "project_finish")
PROJECT_SCHEMAS = [PROJECT_WRITE_SCHEMA, PROJECT_APPEND_SCHEMA, PROJECT_FINISH_SCHEMA]

PROJECT_MANUAL = (
    "WEBSITE / MULTI-FILE PROJECT (project_write, project_append, project_finish): a whole site never fits in one reply, so build it in "
    "pieces. 1) project_write index.html as a short skeleton (head, nav, empty section tags). 2) project_write css/styles.css and "
    "js/main.js (start small). 3) fill each section with project_append, one section per call, under about 150 lines each. "
    "4) project_finish, then reply with a short summary and the two [DOWNLOAD: ...] links it returns. Use the same `project` name in "
    "every call. Link files with relative paths (css/styles.css, js/main.js). The page must read well without external libraries: "
    "CDN scripts are optional extras. Never write card numbers. Pictures are linked by https address, never invented as local files.")

_WANTED = re.compile(
    r"\b(web ?site|web ?app|landing page|portfolio site|multi-?page|front-?end|single[- ]page|index\.html|styles\.css|main\.js|"
    r"project structure|html\s*,?\s*css|css\s*(?:and|,|&)\s*(?:vanilla\s*)?(?:js|javascript))\b", re.I)


def project_wanted(query: str) -> bool:
    """A request for a website or a multi-file front-end project (not a single html page or a report)."""
    return bool(_WANTED.search(str(query or "")))


def _slug(name) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower()).strip("-")[:40]
    return s or "project"


def _root(project) -> Path:
    return _common_resolve(f"{PROJECTS_DIR}/{_slug(project)}")


def _file(project, path) -> tuple:
    """(project folder, target file, clean relative path) or raises ValueError with a message safe for the model."""
    rel = str(path or "").replace("\\", "/").strip().lstrip("/")
    if rel.lower().startswith(("project/", "projects/")):
        rel = rel.split("/", 1)[1]
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if len(parts) > 1 and _slug(parts[0]) == _slug(project):
        parts = parts[1:]                    # "dhaka-site/index.html" in project "dhaka-site": the folder is already the project
    if not parts or ".." in parts or re.match(r"^[A-Za-z]:", parts[0]) or len(parts) > 5:
        raise ValueError("error: path must be a relative file path inside the project, like css/styles.css")
    rel = "/".join(parts)
    if not rel.lower().endswith(TEXT_EXTS):
        raise ValueError(_BAD_EXT)
    root = _root(project)
    target = (root / rel).resolve()
    if root.resolve() not in target.parents:
        raise ValueError("error: path escapes the project folder")
    return root, target, rel


def _size(root: Path) -> tuple:
    files = [f for f in root.rglob("*") if f.is_file()] if root.is_dir() else []
    return len(files), sum(f.stat().st_size for f in files)


def _pan_check(content: str):
    from .. import doc_ops
    from ..doc_ops.base import DocOpError
    try:
        doc_ops._check_ops_for_pan([{"op": "write", "content": content}])
    except DocOpError as e:
        return f"error: write refused: {e}"
    return None


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
    refused = _pan_check(str(content))
    if refused:
        return refused
    existed = target.is_file()
    if append and not existed:
        return f"error: {rel} does not exist yet. Create it with project_write first (a short skeleton), then project_append."
    count, total = _size(root)
    if not existed and count >= MAX_FILES:
        return f"error: a project holds at most {MAX_FILES} files."
    if total + len(str(content).encode("utf-8")) > MAX_PROJECT_BYTES:
        return "error: the project is at its size limit; shorten the content."
    target.parent.mkdir(parents=True, exist_ok=True)
    text = str(content)
    if append:
        old = target.read_text(encoding="utf-8", errors="replace")
        text = old + ("" if old.endswith("\n") else "\n") + text
    with target.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    verb = "Appended to" if append else ("Replaced" if existed else "Created")
    return (f"{verb} {rel} in project {_slug(a.get('project'))} ({len(text.splitlines())} lines, {len(text)} chars). "
            "Continue with the next piece, or call project_finish when every file is written.")


def tool_project_write(args: dict) -> str:
    return _store(args, append=False)


def tool_project_append(args: dict) -> str:
    return _store(args, append=True)


_LINK_CSS = re.compile(r"<link\b[^>]*\brel=[\"']stylesheet[\"'][^>]*>", re.I)
_SCRIPT_SRC = re.compile(r"<script\b([^>]*)\bsrc=[\"']([^\"']+)[\"']([^>]*)>\s*</script>", re.I)
_HREF = re.compile(r"\bhref=[\"']([^\"']+)[\"']", re.I)


def _local(root: Path, ref: str):
    if re.match(r"^(?:[a-z][a-z0-9+.-]*:|//|#)", ref, re.I):
        return None
    p = (root / ref.split("?")[0].split("#")[0].lstrip("./")).resolve()
    return p if p.is_file() and root.resolve() in p.parents else None


def inline_entry(root: Path, entry: str) -> str:
    """The entry page with its local stylesheet and script files written into it, so one file previews like the site."""
    html = (root / entry).read_text(encoding="utf-8", errors="replace")

    def css(m):
        h = _HREF.search(m.group(0))
        p = _local(root, h.group(1)) if h else None
        return f"<style>\n{p.read_text(encoding='utf-8', errors='replace')}\n</style>" if p else m.group(0)

    def js(m):
        p = _local(root, m.group(2))
        if not p:
            return m.group(0)
        body = p.read_text(encoding="utf-8", errors="replace").replace("</script", "<\\/script")
        return f"<script{m.group(1)}{m.group(3)}>\n{body}\n</script>"

    return _SCRIPT_SRC.sub(js, _LINK_CSS.sub(css, html))


def _missing_links(root: Path, entry: str) -> list:
    html = (root / entry).read_text(encoding="utf-8", errors="replace")
    refs = [m.group(1) for m in _HREF.finditer(html)] + re.findall(r"\bsrc=[\"']([^\"']+)[\"']", html, re.I)
    return sorted({r for r in refs if not re.match(r"^(?:[a-z][a-z0-9+.-]*:|//|#|data:)", r, re.I)
                   and r.split("?")[0].split("#")[0].lower().endswith(TEXT_EXTS) and _local(root, r) is None})


def tool_project_finish(args: dict) -> str:
    import uuid
    a = args or {}
    root = _root(a.get("project"))
    if not root.is_dir() or not any(f.is_file() for f in root.rglob("*")):
        return "error: this project has no files yet. Create them with project_write first."
    entry = str(a.get("entry") or "index.html").replace("\\", "/").lstrip("/")
    if not (root / entry).is_file():
        htmls = sorted(p.relative_to(root).as_posix() for p in root.rglob("*.html"))
        if not htmls:
            return "error: the project has no HTML page. Create index.html with project_write."
        entry = "index.html" if "index.html" in htmls else htmls[0]
    slug = _slug(a.get("project"))
    uid8 = uuid.uuid4().hex[:8]
    zip_name, page_name = f"{slug}-{uid8}.zip", f"{slug}-{uid8}.html"
    common = common_workspace()
    files = sorted(f for f in root.rglob("*") if f.is_file())
    with zipfile.ZipFile(common / zip_name, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, f"{slug}/{f.relative_to(root).as_posix()}")
    with (common / page_name).open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(inline_entry(root, entry))
    note = ""
    missing = _missing_links(root, entry)
    if missing:
        note = (" WARNING: the page links to files that do not exist yet: " + ", ".join(missing[:8])
                + ". Create them with project_write and call project_finish again.")
    empty = [f.relative_to(root).as_posix() for f in files if f.stat().st_size == 0]
    if empty:
        note += " WARNING: empty files: " + ", ".join(empty[:8]) + "."
    return (f"Project {slug}: {len(files)} files. Download the whole site as {zip_name} [DOWNLOAD: {zip_name}] and preview "
            f"{entry} as {page_name} [DOWNLOAD: {page_name}]. Reply now with a short summary and these two links." + note)


def open_project_note(max_age_s: int = 3 * 24 * 3600) -> str:
    """System note for a bare "continue" when a website project was left unfinished (stopped, restarted, cut off), else ''.
    A project is open while no ZIP made by project_finish is newer than its files. Never raises."""
    import time
    try:
        base = common_workspace() / PROJECTS_DIR
        if not base.is_dir():
            return ""
        best = None
        for d in base.iterdir():
            files = [f for f in d.rglob("*") if f.is_file()] if d.is_dir() else []
            if not files:
                continue
            newest = max(f.stat().st_mtime for f in files)
            if time.time() - newest > max_age_s:
                continue
            if best is None or newest > best[0]:
                best = (newest, d, files)
        if not best:
            return ""
        newest, d, files = best
        if any(z.stat().st_mtime >= newest for z in common_workspace().glob(f"{d.name}-*.zip")):
            return ""
        listing = ", ".join(f"{f.relative_to(d).as_posix()} ({len(f.read_text(encoding='utf-8', errors='replace').splitlines())} lines)"
                            for f in sorted(files)[:40])
        return (f"[system note] The user's website project `{d.name}` was interrupted before project_finish. Files so far: {listing}. "
                f"Continue it now by tool calls only (never paste code into the reply): project_write the files that are still missing "
                f"(use project=`{d.name}`), project_append to extend a file that is cut short, do not rewrite complete files, and "
                "call project_finish when every file is written.")
    except Exception:
        return ""
