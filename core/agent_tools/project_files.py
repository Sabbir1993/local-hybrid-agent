"""Chat project builder: a multi-file static site (html / css / js / json / md / svg) written in pieces, then one ZIP.

A reply is cut off by the output limit long before a whole website fits in one write_file call, so the model builds it the way a
person would: project_write one file at a time, project_append the next section of a long file, project_finish to get the ZIP and
a single-file preview. Nothing here runs: files are inert text in the user's own common space (never another user's), the same
folder the other chat documents use. Card numbers are refused on every write, like every other document the chat saves."""
import contextvars
from html.parser import HTMLParser
import json
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
    """Called once per chat request (like set_current_user); tools run in a copy of this context."""
    try:
        _scope_var.set(f"s{int(session_id)}")
    except (TypeError, ValueError):
        _scope_var.set("anon")


def _scope() -> str:
    return _scope_var.get()


def _root(project) -> Path:
    return _common_resolve(f"{PROJECTS_DIR}/{_scope()}/{_slug(project)}")


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
    clean, seen = [], set()
    for item in files:
        if isinstance(item, str):
            item = {"path": item, "purpose": ""}
        if not isinstance(item, dict):
            continue
        try:
            _, _, rel = _file(a.get("project"), item.get("path"))
        except ValueError as e:
            return str(e)
        if rel in seen:
            continue
        seen.add(rel)
        try:
            need = max(1, min(400, int(item.get("min_lines") or 5)))
        except (TypeError, ValueError):
            need = 5
        clean.append({"path": rel, "purpose": str(item.get("purpose") or "")[:140], "min_lines": need})
    if len(clean) > MAX_PLAN_FILES:
        return f"error: plan at most {MAX_PLAN_FILES} files; merge small ones (one css file, one or two js files)."
    if not any(f["path"].lower().endswith((".html", ".htm")) for f in clean):
        return "error: the plan has no HTML page. Add index.html."
    pp = _plan_path(a.get("project"))
    pp.parent.mkdir(parents=True, exist_ok=True)
    with pp.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump({"files": clean}, fh, ensure_ascii=False)
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


class _TagStackParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
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
    cleaned = re.sub(r"<script\b[\s\S]*?</script>", "", html_str, flags=re.I)
    cleaned = re.sub(r"<style\b[\s\S]*?</style>", "", cleaned, flags=re.I)
    parser = _TagStackParser()
    try:
        parser.feed(cleaned)
    except Exception:
        pass
    return parser.stack


def _find_first_html_tag(snippet: str):
    """Returns (is_closing: bool, tag_name: str) or None."""
    s = re.sub(r"^\s*<!--[\s\S]*?-->\s*", "", snippet)
    m = re.match(r"^\s*<(/)?([a-zA-Z0-9:-]+)", s)
    if m:
        return (bool(m.group(1)), m.group(2).lower())
    return None


def _calculate_auto_closing_tags(open_stack: list[str], incoming_snippet: str) -> list[str]:
    """Calculate tags that must be closed before incoming_snippet is appended."""
    if not open_stack:
        return []

    usable_stack = [t for t in open_stack if t not in ("html", "body")]
    if not usable_stack:
        return []

    first_tag_info = _find_first_html_tag(incoming_snippet)
    if not first_tag_info:
        return []

    is_closing, tag = first_tag_info

    curr_stack = list(usable_stack)
    if is_closing:
        s = re.sub(r"^\s*<!--[\s\S]*?-->\s*", "", incoming_snippet)
        for m in re.finditer(r"</([a-zA-Z0-9:-]+)>", s):
            end_t = m.group(1).lower()
            if curr_stack and curr_stack[-1] == end_t:
                curr_stack.pop()
            else:
                break
        return []

    tags_to_close = []

    # Case 1: Incoming is a sectioning element (section, header, footer, article, nav, aside)
    if tag in _HTML_SECTIONING_TAGS:
        found_idx = -1
        for i in range(len(curr_stack) - 1, -1, -1):
            if curr_stack[i] in _HTML_SECTIONING_TAGS:
                found_idx = i
                break
        if found_idx != -1:
            tags_to_close = curr_stack[found_idx:]
            tags_to_close.reverse()
            return tags_to_close
        else:
            while curr_stack and (curr_stack[-1] in _HTML_INLINE_TAGS or curr_stack[-1] in _HTML_HEADING_TAGS or curr_stack[-1] == "p"):
                tags_to_close.append(curr_stack.pop())
            return tags_to_close

    # Case 2: Incoming is a block element, heading, or paragraph
    if tag in _HTML_BLOCK_TAGS or tag in _HTML_HEADING_TAGS or tag == "p":
        while curr_stack and (curr_stack[-1] in _HTML_INLINE_TAGS or curr_stack[-1] in _HTML_HEADING_TAGS or curr_stack[-1] == "p"):
            tags_to_close.append(curr_stack.pop())
        return tags_to_close

    return tags_to_close


def _balance_html_document(html_str: str) -> str:
    """Ensure all open tags inside <body> are closed before </body> or </html>."""
    m_body = re.search(r"</body\b[^>]*>", html_str, re.I)
    if not m_body:
        return html_str
    idx = m_body.start()
    before = html_str[:idx]
    after = html_str[idx:]

    m_script = re.search(r"(<script\b[\s\S]*?</script>\s*)$", before, re.I)
    trailing_script = ""
    if m_script and len(m_script.group(1)) < 3000:
        s_idx = m_script.start()
        trailing_script = before[s_idx:]
        before = before[:s_idx]

    open_tags = _get_open_tags(before)
    dangling = [t for t in open_tags if t not in ("html", "body")]
    if dangling:
        close_html = "\n" + "\n".join(f"</{t}>" for t in reversed(dangling))
        before = before.rstrip() + close_html + "\n"

    return before + trailing_script + after


def _smart_html_append(old: str, new_content: str) -> str:
    """Intelligently stitch an HTML fragment into an existing HTML document.

    Auto-closes unclosed/dangling tags from previous chunks (such as <p>, <div>, <section>)
    when an appended chunk introduces a new section or block element, preventing broken DOM trees.
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

    # 3. Locate injection boundaries
    before_body, after_body = old, ""
    m_body = re.search(r"</body\b[^>]*>", old, re.I)
    if m_body:
        idx = m_body.start()
        before_body = old[:idx]
        after_body = old[idx:]
        m_script = re.search(r"(<script\b[\s\S]*?</script>\s*)$", before_body, re.I)
        if m_script and len(m_script.group(1)) < 3000:
            s_idx = m_script.start()
            after_body = before_body[s_idx:] + after_body
            before_body = before_body[:s_idx]
    else:
        m_html = re.search(r"</html\b[^>]*>", old, re.I)
        if m_html:
            idx = m_html.start()
            before_body = old[:idx]
            after_body = old[idx:]

    # 4. Fix dangling unclosed markdown link at the end of before_body (e.g. `[2](https://...`)
    b_trimmed = before_body.rstrip()
    if re.search(r"\[[^\]]+\]\((?:https?://)?[^\s\)]*$", b_trimmed):
        before_body = b_trimmed + ")"

    # 5. Fix dangling incomplete opening tag (e.g. cut off `<div class="` at end)
    b_trimmed = before_body.rstrip()
    if re.search(r"<[a-zA-Z0-9:-]+(?:\s+[^>\"']*?(?:=\"[^\"]*\"|='[^']*')?)*\s*$", b_trimmed):
        m_tag = re.search(r"(<[a-zA-Z0-9:-]+(?:\s+[^>\"']*?(?:=\"[^\"]*\"|='[^']*')?)*\s*)$", b_trimmed)
        if m_tag and not m_tag.group(1).endswith(">"):
            before_body = b_trimmed[:m_tag.start()]

    # 6. Auto-close dangling tags from previous chunk if new chunk starts a section or block
    open_tags = _get_open_tags(before_body)
    to_close = _calculate_auto_closing_tags(open_tags, cleaned)
    if to_close:
        close_html = "\n".join(f"</{t}>" for t in to_close)
        before_body = before_body.rstrip() + "\n" + close_html

    # 7. Assemble
    if after_body:
        return before_body.rstrip() + "\n\n" + cleaned + "\n\n" + after_body.lstrip()
    return before_body.rstrip() + ("\n\n" if not before_body.endswith("\n\n") else "") + cleaned + "\n"


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
    if not _load_plan(a.get("project")) and not (root.is_dir() and any(root.rglob("*"))):
        return ("error: plan the site first: call project_plan with the complete list of files (at most 14, each with a one-line "
                "purpose), then write them one at a time.")
    existed = target.is_file()
    if existed and not append:
        try:
            curr_text = target.read_text(encoding="utf-8", errors="replace")
            if curr_text.strip() == str(content).strip():
                nxt = _progress(a.get("project")).strip() or "Call project_finish when all planned files are written."
                return (f"note: {rel} already has this exact content ({_lines(target)} lines). Do not rewrite identical files. "
                        f"Use project_append to add new sections, or call project_finish if complete. {nxt}")
        except Exception:
            pass
        if not a.get("replace"):
            done = {f["path"] for f in plan_status(a.get("project"))["done"]}
            plan = _load_plan(a.get("project"))
            if rel in done or not plan or _lines(target) >= 10:
                nxt = _progress(a.get("project")).strip() or "Call project_finish if every file is written."
                return (f"error: {rel} is already written ({_lines(target)} lines): do not overwrite it again. "
                        f"Use project_append to extend it, or call project_finish if complete. "
                        f"(Pass replace=true only if redoing from scratch). {nxt}")
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
        if rel.lower().endswith((".html", ".htm")):
            text = _smart_html_append(old, text)
        else:
            text = old + ("" if old.endswith("\n") else "\n") + text
    with target.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    verb = "Appended to" if append else ("Replaced" if existed else "Created")
    unplanned = ""
    plan = _load_plan(a.get("project"))
    if plan and rel not in {f["path"] for f in plan}:
        unplanned = " (this file is not in the plan: fine, or add it with project_plan)"
    return (f"{verb} {rel} in project {_slug(a.get('project'))} ({len(text.splitlines())} lines, {len(text)} chars){unplanned}."
            + (_progress(a.get("project")) or " Continue with the next piece, or call project_finish when every file is written."))


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
    html = _balance_html_document(html)

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
    st = plan_status(a.get("project"))
    if st["todo"]:
        return ("error: the plan is not finished: " + "; ".join(f"{f['path']} ({f.get('purpose', '')})" for f in st["todo"][:10])
                + ". Write these with project_write (project_append to lengthen a short file), or drop one with project_plan, then call "
                "project_finish again.")
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
    # Ensure all HTML files are properly closed and balanced before packaging
    for f in files:
        if f.suffix.lower() in (".html", ".htm"):
            try:
                raw_h = f.read_text(encoding="utf-8", errors="replace")
                bal_h = _balance_html_document(raw_h)
                if bal_h != raw_h:
                    f.write_text(bal_h, encoding="utf-8", newline="\n")
            except Exception:
                pass
    single = len(files) == 1      # one html page: a ZIP of one file is noise, hand over just the page
    if not single:
        with zipfile.ZipFile(common / zip_name, "w", zipfile.ZIP_DEFLATED) as z:
            for f in files:
                z.write(f, f"{slug}/{f.relative_to(root).as_posix()}")
    from ..html_quality import repair as _repair_html
    page_html, typos = _repair_html(inline_entry(root, entry))      # near-miss class names and stray tags a small model leaves behind
    with (common / page_name).open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(page_html)
    note = f" (fixed {typos} typo(s) in the page's class names and tags automatically)" if typos else ""
    missing = _missing_links(root, entry)
    if missing:
        note = (" WARNING: the page links to files that do not exist yet: " + ", ".join(missing[:8])
                + ". Create them with project_write and call project_finish again.")
    empty = [f.relative_to(root).as_posix() for f in files if f.stat().st_size == 0]
    if empty:
        note += " WARNING: empty files: " + ", ".join(empty[:8]) + "."
    if single:
        return f"Page {slug}: 1 file. Download it as {page_name} [DOWNLOAD: {page_name}]. Reply now with a short summary and this link." + note
    return (f"Project {slug}: {len(files)} files. Download the whole site as {zip_name} [DOWNLOAD: {zip_name}] and preview "
            f"{entry} as {page_name} [DOWNLOAD: {page_name}]. Reply now with a short summary and these two links." + note)


def open_project_note(max_age_s: int = 3 * 24 * 3600) -> str:
    """System note for a bare "continue" when a website project was left unfinished (stopped, restarted, cut off), else ''.
    A project is open while no ZIP made by project_finish is newer than its files. Never raises."""
    import time
    try:
        base = common_workspace() / PROJECTS_DIR / _scope()
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
