import re
from pathlib import Path
from typing import Optional
from core.agent_loop import estimate_prompt_tokens, compact_messages


WEB_TOOL_NAMES = ("web_search", "web_fetch", "web_search_images")


# A deliverable request: a creation verb near a file-format / artifact noun.
_FILE_VERB_RE = r'(fill|write|save|create|generate|make|export|download|share|prepare|build|draft|compile)\w*'


_FILE_NOUN_RE = (r'(excel|spreadsheet|workbook|csv|\.xlsx|\.xls|\.csv|\.json|\.py|\.html|html|\.txt|\.docx'
                 r'|\.pptx|\.ppt|\.pdf|pdf|presentation|slides|deck|dashboard|web ?page)\b')


# "Let me compile the HTML now:" - the model announced work it then didn't do.
_ANNOUNCE_RE = re.compile(
    r"\b(let me|i will|i'll|i am going to|i'm going to|now i(?:'ll| will)?)\s+(?:now\s+)?(?:actually\s+)?"
    r"(compile|create|generate|write|prepare|build|put together|draft|make|produce"
    r"|finali[sz]e|rebuild|redesign|polish|update|regenerate|redo|rewrite|call write_file)\b",
    re.IGNORECASE)


# Follow-ups about a file produced earlier in this chat. The frontend only
# resends assistant text (big code blocks stubbed, tool args never sent), so
# without help the model can't see the old file and invents a new one.
_DL_TAG_RE = re.compile(r"\[DOWNLOAD:\s*([^\]]+)\]")


_FILE_REF_RE = r"(file|report|html|page|dashboard|document|doc|pdf|sheet|excel|csv|deck|slides|presentation|link)"


_FILE_WHERE_RE = re.compile(
    r"\b(where(?:'s| is)?|link|download|re-?share|resend|send|give|share)\b.{0,30}\b" + _FILE_REF_RE + r"\b"
    r"|\b" + _FILE_REF_RE + r"\b.{0,15}\b(where|link|missing|not (?:shared|found|there))\b",
    re.IGNORECASE)


_FILE_EDIT_RE = re.compile(
    r"\b(edit|update|modify|change|improve|polish|fix|redesign|rebuild|redo|rewrite|restyle|revise|refine"
    r"|add|remove|replace|rename|translate|enhance)\w*\b",
    re.IGNORECASE)


_TEXT_FILE_EXTS = {".html", ".htm", ".css", ".js", ".ts", ".json", ".csv", ".md", ".txt", ".py",
                   ".xml", ".svg", ".sql", ".yaml", ".yml"}


PRIOR_FILE_MAX_CHARS = 60000


_ATTACHED_DOC_RE = re.compile(r"--- FILE:\s*([^\n]+?\.(?:pptx|xlsx|docx|csv|pdf))\s*---", re.IGNORECASE)


# edited through doc_inspect/doc_edit (in place, by address) rather than rewritten
_DOC_EDIT_EXTS = {".pptx", ".xlsx", ".docx", ".csv", ".pdf"}


def session_files(msgs: list) -> list:
    """Filenames the server delivered (or the user attached) in this conversation, oldest first."""
    seen = []
    for m in msgs:
        role = m.get("role")
        text = str(m.get("content") or "")
        if role == "user":
            names = _ATTACHED_DOC_RE.findall(text)
        elif role == "assistant":
            names = _DL_TAG_RE.findall(text)
        else:
            continue
        for name in names:
            n = Path(name.strip()).name
            if n in seen:
                seen.remove(n)
            seen.append(n)
    return seen


def file_followup_intent(query: str, has_prior: bool) -> Optional[str]:
    """'where' = user can't find / wants the existing file again;
    'edit' = user wants the existing file changed. None otherwise."""
    q = (query or "").strip()
    if not has_prior or not q:
        return None
    if _FILE_WHERE_RE.search(q) and not _FILE_EDIT_RE.search(q):
        return "where"
    if _FILE_EDIT_RE.search(q) and re.search(
            r"\b" + _FILE_REF_RE + r"\b|\b(it|this|that|previous|last|same|ui|design|style|colou?rs?|layout"
            r"|section|chart|table|theme|font)\b|/frontend-design", q, re.IGNORECASE):
        return "edit"
    return None


def load_prior_file(name: str, max_chars: int) -> Optional[str]:
    """Text content of a common-space file this chat produced, or None
    (missing, binary format, or too big to fit the context budget)."""
    from core.agent_tools import _common_resolve
    try:
        p = _common_resolve(name)
    except PermissionError:
        return None
    if p.suffix.lower() not in _TEXT_FILE_EXTS or not p.is_file():
        return None
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return text if len(text) <= max_chars else None


def wants_file_output(query: str) -> bool:
    return bool(re.search(_FILE_VERB_RE + r'.{0,40}' + _FILE_NOUN_RE, query or "", re.IGNORECASE)
                or re.search(_FILE_NOUN_RE + r'.{0,25}' + _FILE_VERB_RE, query or "", re.IGNORECASE))


def looks_undelivered(content: str, wants_file: bool, delivered: bool) -> bool:
    """True when a text-only reply promised a deliverable but contains none."""
    if delivered:
        return False
    c = (content or "").strip()
    if "```" in c or "<html" in c.lower():
        return False
    if wants_file:
        return True
    return c.endswith(":") or bool(_ANNOUNCE_RE.search(c[-300:]))


def shrink_old_tool_results(msgs: list, budget_tokens: int, keep_last: int = 2, head_chars: int = 800) -> None:
    """Research loops pile up 6-20 KB web results per call. Once the prompt
    passes the budget, cut older tool results down to their head (the latest
    `keep_last` stay whole), then fall back to digest compaction."""
    if estimate_prompt_tokens(msgs) <= budget_tokens:
        return
    tool_idx = [i for i, m in enumerate(msgs) if m.get("role") == "tool"]
    for i in tool_idx[:-keep_last] if keep_last else tool_idx:
        c = str(msgs[i].get("content") or "")
        if len(c) > head_chars:
            msgs[i]["content"] = c[:head_chars] + f"\n...[{len(c) - head_chars} chars trimmed to fit context]"
    if estimate_prompt_tokens(msgs) > budget_tokens:
        msgs[:] = compact_messages(msgs, budget_tokens)
