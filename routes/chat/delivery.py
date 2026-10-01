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
    return [f for f in cands if f in latest.values()]


def _finalize_download_tags(content: str, written: list[str], query: str) -> tuple[str, bool]:
    """Drop [DOWNLOAD:] tags for superseded files written this turn and append
    tags for deliverables the reply doesn't link yet. Returns (content, changed)."""
    keep = _pick_deliverables(written, query)
    out = content or ""
    for f in set(written) - set(keep):
        out = re.sub(rf"[ \t]*\[DOWNLOAD:\s*{re.escape(f)}\s*\][ \t]*\n?", "", out)
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
