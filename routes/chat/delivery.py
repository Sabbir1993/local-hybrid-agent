import re
from pathlib import Path
from core.agent_loop import estimate_prompt_tokens


def _saved_filename(res_str: str, fallback: str) -> str:
    """tool_write_file_common() renames every file with a unique suffix; pull
    the real on-disk name back out of its result string (via the [DOWNLOAD: ..]
    tag it always includes) so callers don't keep referencing the pre-write name."""
    m = re.search(r"\[DOWNLOAD:\s*([^\]]+)\]", res_str or "")
    return m.group(1).strip() if m else fallback


def _prompt_tokens_of(res_dict, msgs, tools=None) -> int:
    """Full prompt size of the last LLM call, cached tokens included.
    llama.cpp's timings.prompt_n counts only freshly-evaluated tokens, so add
    cache_n; a count far below the estimate is cache-excluded - use the estimate.
    tools are the schemas sent with that call: omitting them undercounts by the
    thousands of tokens the schema block costs (the exact tool-blind bug
    core/context_budget.py exists to eliminate)."""
    est = estimate_prompt_tokens(msgs, tools)
    u = (res_dict or {}).get("usage") or {}
    t = (res_dict or {}).get("timings") or {}
    real = int(u.get("prompt_tokens") or 0)
    if t:
        real = max(real, int(t.get("prompt_n") or 0) + int(t.get("cache_n") or 0))
    return real if real >= est * 0.5 else est


_HELPER_EXTS = {".py", ".js", ".ts", ".sh", ".bat", ".ps1"}


def _requested_exts(query: str) -> set[str]:
    """File extensions the user's message asks for (empty = not stated)."""
    q = (query or "").lower()
    exts = {"." + e for e in re.findall(r"\.(pptx|ppt|xlsx|xls|csv|docx|pdf|html|json|txt|md|py)\b", q)}
    if re.search(r"\bpdf\b", q):
        exts.add(".pdf")
    if re.search(r"\b(pptx?|powerpoint)\b", q) or (
            re.search(r"\b(slides?|deck|presentation)\b", q) and ".pdf" not in exts):
        exts.add(".pptx")
    if re.search(r"\b(excel|spreadsheet|workbook)\b", q):
        exts.add(".xlsx")
    if re.search(r"\bcsv\b", q):
        exts.add(".csv")
    if re.search(r"\b(word|docx)\b", q):
        exts.add(".docx")
    if re.search(r"\b(html|dashboard|web ?page)\b", q):
        exts.add(".html")
    return {".pptx" if e == ".ppt" else ".xlsx" if e == ".xls" else e for e in exts}


def _pick_deliverables(written: list[str], query: str) -> list[str]:
    """A turn may write several drafts (v1, v2, _final, ...) and helper scripts
    before it lands the file. Only the latest file of each requested type is the
    deliverable; everything else stays on disk but gets no download badge."""
    uniq = list(dict.fromkeys(written))
    if len(uniq) <= 1:
        return uniq
    want = _requested_exts(query)
    cands = [f for f in uniq if Path(f).suffix.lower() in want] if want else []
    if not cands:
        cands = [f for f in uniq if Path(f).suffix.lower() not in _HELPER_EXTS] or uniq
    latest: dict[str, str] = {}
    for f in cands:  # later writes supersede earlier ones of the same type
        latest[Path(f).suffix.lower()] = f
    picked = [f for f in cands if f in latest.values()]
    # a project's ZIP (project_finish) is always a deliverable next to its preview page, whatever type the request names
    return picked + [f for f in uniq if f.lower().endswith(".zip") and f not in picked]


def _misspelled_tags(content: str, written: list[str], known: list[str]) -> list[str]:
    """Tags in the reply that name no real file but are a near miss of one written this turn: a model copying a random
    file name ("report-6cb.4a5dc.html" for "report-6cb4a5dc.html") gets one character wrong, and the link is dead."""
    import difflib
    real = set(written) | set(known or [])
    bad = []
    for name in re.findall(r"\[DOWNLOAD:\s*([^\]\n\[]+)\]", content or ""):
        name = name.strip()
        if name in real:
            continue
        near = [w for w in written if Path(w).suffix.lower() == Path(name).suffix.lower()
                and difflib.SequenceMatcher(None, name.lower(), w.lower()).ratio() >= 0.75]
        if near:
            bad.append(name)
    return bad


def _finalize_download_tags(content: str, written: list[str], query: str, known: list[str] = ()) -> tuple[str, bool]:
    """Drop [DOWNLOAD:] tags for superseded files written this turn and append
    tags for deliverables the reply doesn't link yet. Returns (content, changed)."""
    keep = _pick_deliverables(written, query)
    out = content or ""
    for name in _misspelled_tags(out, written, list(known or [])):
        out = re.sub(rf"[ \t]*\[DOWNLOAD:\s*{re.escape(name)}\s*\][ \t]*\n?", "", out)
    # a tag the model cut off ("[DOWNLOAD: report-ab12" with no "]") is junk that would swallow the real one below it
    out = re.sub(r"[ \t]*\[DOWNLOAD:[^\]\n\[]*(?=\n|$|\[)", "", out)
    for f in set(written) - set(keep):
        out = re.sub(rf"[ \t]*\[DOWNLOAD:\s*{re.escape(f)}\s*\][ \t]*\n?", "", out)
    # an invented address for a file the app already links ("(https://example.com/site-ab12.zip)") is dead: drop it
    for f in set(written):
        out = re.sub(r"[ \t]*\(?<?https?://[^\s)>\]]*" + re.escape(f) + r">?\)?", "", out)
    out = re.sub(r"\n{3,}", "\n\n", out).rstrip()
    for f in keep:
        if f"[DOWNLOAD: {f}]" not in out and f"download?path={f}" not in out.lower():
            out += f"\n\n[DOWNLOAD: {f}]"
    return out, out != (content or "")


def _finalize_media(content: str, made: list[str]) -> tuple[str, bool]:
    """Pictures/videos made this turn are always shown: append each result's
    markdown line (image / [VIDEO:] / [DOWNLOAD:]) the reply doesn't carry yet.
    The model may drop or garble it, and it only saw a copy of the path."""
    out = (content or "").rstrip()
    for md in made:
        for line in (ln.strip() for ln in md.split("\n")):
            m = re.search(r"path=([^)\s]+)\)", line)          # an image: its path; a tag: the tag
            if line and (m.group(1) if m else line) not in out:
                out += "\n\n" + line
    return out, out != (content or "")


def _wraps_media(made: list[str], fname: str, query: str) -> bool:
    """An HTML page written after an image this turn is the model wrapping the
    picture (with a link it may have garbled) instead of showing it - unless the
    user asked for a page."""
    return bool(made) and Path(fname or "").suffix.lower() in (".html", ".htm") \
        and ".html" not in _requested_exts(query)


_WRAP_REFUSAL = ("error: the picture is already saved and shown to the user - don't put it in an HTML page "
                 "or any other file. Reply with the markdown generate_image returned and one short sentence.")


# Chat saves documents, not programs: nothing here can run them, and a saved script is one a user may later run by hand.
# Agent mode (approval prompts, device workspace) is where code gets written to disk.
EXECUTABLE_SUFFIXES = (".sh", ".bash", ".zsh", ".bat", ".cmd", ".ps1", ".psm1", ".py", ".pyw", ".vbs", ".wsf", ".exe", ".msi",
                       ".com", ".scr", ".pif", ".dll", ".lnk", ".jar", ".reg", ".hta")
_SCRIPT_REFUSAL = ("error: chat does not save scripts or programs (nothing here can run them). For a deck / PDF use doc_create "
                   "then doc_append; for code, show it to the user in a fenced code block, or tell them Agent mode can write and run it.")
_NO_TOOL_ANSWER = ("I could not finish that in this chat turn. Please say \"continue\" and I will carry on from where it stopped "
                   "(Agent mode can run scripts if you need that).")


def is_executable_path(path) -> bool:
    return str(path or "").strip().lower().endswith(EXECUTABLE_SUFFIXES)


def chat_write_file(args: dict) -> str:
    """tool_write_file_common for chat: refuses executable file types (see EXECUTABLE_SUFFIXES)."""
    from core.agent_tools import tool_write_file_common
    a = args or {}
    if is_executable_path(a.get("path") or a.get("file") or a.get("filename")):
        return _SCRIPT_REFUSAL
    return tool_write_file_common(args)


_ALREADY_SAVED = ("error: this document is already saved as {name} and the user can download it. Do NOT write it again: "
                  "reply now with a short summary and the [DOWNLOAD: {name}] link.")


def _already_written(written_files: list, args: dict) -> bool:
    """True when this write_file would save the same document (same base name and type) again in one reply."""
    from core.doc_store import base_stem
    from pathlib import Path
    p = str((args or {}).get("path") or (args or {}).get("file") or (args or {}).get("filename") or "")
    if not p or not written_files:
        return False
    want = (base_stem(Path(p).name).lower(), Path(p).suffix.lower())
    return any((base_stem(Path(f).name).lower(), Path(f).suffix.lower()) == want for f in written_files)


# The server's own file locations are never shown to a user: the model invents "(/common_space/report.xlsx)" links from the
# phrase "common space" in a tool result, and a path like that tells the reader how the server is laid out. A file is shared
# only through its [DOWNLOAD: name] tag, which the app turns into a preview / download button.
_DOC_EXT = r"(?:xlsx|xls|csv|pptx|ppt|docx|doc|pdf|md|txt|html|json|png|jpg|jpeg|zip)"
_SERVER_PATH = (
    r"(?:sandbox:|file:///?)?(?:[A-Za-z]:[\\/]|/)?(?:[^\s()<>\]\[`\"']*[\\/])?(?:common_space|user_\d+|uploads|knowledge_uploads)[\\/]"
    r"[^\s()<>\]\[`\"']*")
_SANDBOX_PATH = r"(?:sandbox:)?/mnt/(?:data|user-data|outputs)/[^\s()<>\]\[`\"']+"
_ABS_PATH = r"(?:[A-Za-z]:[\\/]|/(?:tmp|home|var|srv|mnt|opt|etc|usr|root|Users|data)/)[^\s()<>\]\[`\"']+"


def _base(path: str) -> str:
    return re.split(r"[\\/]", path.strip().rstrip("/\\"))[-1]


def scrub_server_paths(content: str) -> str:
    """Replace server file paths in a reply by the bare file name: a markdown link to a server path becomes a
    [DOWNLOAD: name] tag (documents) or plain text, and a bare path into the server's file areas (common_space, user_<id>,
    uploads) becomes the file name. Fenced code is left alone, and ordinary absolute paths a user may be asking about
    (/etc/hosts, C:/Windows) are only touched when they are the target of a link to a document."""
    text = content or ""
    if not text:
        return text

    def link(m):
        label, target = m.group(1), m.group(2)
        name = _base(target)
        if re.search(r"\." + _DOC_EXT + r"$", name, re.I):
            return f"[DOWNLOAD: {name}]"
        return label or name

    link_rx = re.compile(r"!?\[([^\]]*)\]\(\s*((?:" + _SERVER_PATH + r"|" + _SANDBOX_PATH + r"|" + _ABS_PATH + r"))\s*\)", re.I)
    bare_rx = re.compile(r"`?(" + _SERVER_PATH + r"|" + _SANDBOX_PATH + r")`?", re.I)
    parts = re.split(r"(```[\s\S]*?```)", text)
    for i, part in enumerate(parts):
        if part.startswith("```"):
            continue
        part = link_rx.sub(link, part)
        part = bare_rx.sub(lambda m: _base(m.group(1)), part)
        parts[i] = re.sub(r"(\[DOWNLOAD: [^\]]+\])(?:\s*\(\s*\1\s*\))+", r"\1", part)
    return "".join(parts)


_DUPLICATE_WORKBOOK = ("error: this chat already has the workbook {name}. Change THAT file instead of creating another one: "
                       "doc_edit(file='{name}', ops=[...]) with set_column (one column of one sheet), set_range / set_cell, "
                       "add_outline_sheet (a new sheet), add_column, set_format, add_chart ... Only the parts the user asked for "
                       "change. Create a new file only if the user asked for a separate workbook.")
_SEPARATE_RE = re.compile(
    r"\b(new|another|separate|second|different|fresh|brand new|merge|merged|combining|combine|consolidate|consolidated|deliverable|join|joined)\b",
    re.I,
)


def _duplicate_workbook(files: list, args: dict, query: str) -> bool:
    """True when this call would create a second .xlsx although the chat already has one and the user did not ask for a new one."""
    p = str((args or {}).get("path") or (args or {}).get("file") or (args or {}).get("filename") or "").lower()
    if not p.endswith((".xlsx", ".xls")) or _SEPARATE_RE.search(query or ""):
        return False
    wb_files = [f for f in files if str(f).lower().endswith((".xlsx", ".xls"))]
    if len(wb_files) > 1:
        return False
    return bool(wb_files)


_TAG_RX = re.compile(r"[ \t]*\[DOWNLOAD:\s*([^\]\n\[]+)\][ \t]*")


def verify_download_tags(content: str, written: list[str], known: list[str], exists) -> tuple[str, bool]:
    """Every [DOWNLOAD: name] tag must name a file that exists (`exists(name)` -> bool), or the page draws a Preview /
    Download chip for a file that was never made. A name that matches no file is pointed at the one file of the same
    type written this turn, if there is exactly one; otherwise the tag is dropped and the reply says so.
    `known` (names from earlier replies' tags) is NOT trusted on its own: a tag a past reply carried for a page that was
    never saved would otherwise keep drawing a dead Preview / Download button."""
    real = set(written or [])
    out, changed, missing = content or "", False, []

    def fix(m):
        nonlocal changed
        name = m.group(1).strip()
        if name in real or exists(name):
            return m.group(0)
        same = [w for w in dict.fromkeys(written or []) if Path(w).suffix.lower() == Path(name).suffix.lower()]
        changed = True
        if len(same) == 1:
            return f" [DOWNLOAD: {same[0]}] "
        missing.append(name)
        return ""

    out = _TAG_RX.sub(fix, out)
    if missing:
        out = out.rstrip() + "\n\n*(No file was created for " + ", ".join(f"`{n}`" for n in dict.fromkeys(missing)) + \
              " - please ask again.)*"
    return out, changed


_HTML_FENCE_RX = re.compile(r"```html[^\n]*\n([\s\S]*?)\n```", re.I)
_HTML_DOC_RX = re.compile(r"(?:<!DOCTYPE\s+html[^>]*>\s*)?<html\b[\s\S]*?</html>", re.I)   # DOCTYPE optional; a cut-off page has no </html>
_NAMED_HTML_RX = re.compile(r"`?([\w\-]{2,60}\.html?)`?", re.I)


def inline_html_deliverable(content: str, query: str):
    """The model pasted one complete HTML page into the reply instead of saving it. -> (filename, html) or None.
    The name is the first .html the reply or the request mentions, else a slug of the request."""
    if not content or ".html" not in _requested_exts(query) and not _NAMED_HTML_RX.search(content):
        return None
    fences = _HTML_FENCE_RX.findall(content)
    docs = [d.strip() for d in (_HTML_DOC_RX.findall("\n".join(fences)) if fences else [])]
    if len(docs) != 1 or len(docs[0]) < 400:
        return None
    m = _NAMED_HTML_RX.search(content) or _NAMED_HTML_RX.search(query or "")
    if m:
        name = m.group(1)
    else:
        words = re.findall(r"[a-z0-9]+", (query or "").lower())
        name = "-".join(w for w in words if w not in {"make", "a", "an", "the", "with", "me", "create", "build"})[:40] or "page"
        name += ".html"
    return Path(name).name, docs[0]


def _file_exists(name: str) -> bool:
    """True when the Preview / Download buttons for this name would open a file: the same lookup /agent/raw and
    /agent/download use (the exact name, or its newest saved version such as name-1a2b3c4d.html)."""
    from routes.agent.download import _resolve_requested_file
    try:
        p = _resolve_requested_file(Path(name.strip()).name, space="common", recover=False)
        return bool(p and p.is_file())
    except Exception:
        return False


def finalize_reply_files(content: str, written: list[str], query: str, known: list[str], can_write: bool) -> tuple[str, bool]:
    """Last pass over a finished reply, so the page never shows a file link for a file that does not exist:
    1. one complete HTML page pasted into the reply and never saved is saved now (`written` gets the file);
    2. every [DOWNLOAD: x] tag must name a real file (see verify_download_tags);
    3. links for the files written this turn are added / superseded ones dropped (_finalize_download_tags)."""
    out = content or ""
    # a page pasted into the reply is saved unless an HTML file was already written this turn (another kind of file,
    # e.g. a cleaned sheet, does not stand in for the page)
    if can_write and not any(str(w).lower().endswith((".html", ".htm")) for w in written):
        found = inline_html_deliverable(out, query)
        if found:
            res = chat_write_file({"path": found[0], "content": found[1]})
            if not res.startswith("error:"):
                written.append(_saved_filename(res, found[0]))
                out = _TAG_RX.sub("", out)          # the model's own tags named invented files; the right one is added below
    out, _ = verify_download_tags(out, written, known, _file_exists)
    if written:
        out, _ = _finalize_download_tags(out, written, query, known)
    return out, out != (content or "")
