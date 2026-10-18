import re
from pathlib import Path
from typing import Optional
from core.agent_loop import estimate_prompt_tokens, compact_messages
from core.prompt_scope import WEB_TOOL_NAMES


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


def _norm_name(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[-_.]+", " ", str(text or "").lower())).strip()


_TYPE_WORDS = (
    (r"pdf", (".pdf",)),
    (r"sheet|sp[a-z]{2,8}\s?sheet|spreadsheet|excel|xlsx|csv", (".xlsx", ".csv")),
    (r"word|docx|document", (".docx",)),
    (r"deck|ppt|pptx|powerpoint|presentation|slides?", (".pptx",)),
)


def pick_target(query: str, files: list) -> str:
    """Which of this chat's files an edit / "where is it" request is about. The old rule was always the newest one, so a
    user with three decks in a chat could not change the first. Order of evidence:
    1. the message names a file (its name, or its name without the version / id suffix) -> that file's newest version;
    2. a word that only ONE deck's name contains ("the Dhaka deck") -> that deck's newest version;
    3. a type word ("the pdf", "the presentation"), with "previous / earlier / other" picking the one before the newest;
    4. otherwise the newest file."""
    if not files:
        return ""
    from core import doc_store
    q = _norm_name(query)
    bases: dict = {}
    for f in files:                                           # oldest first -> the last entry per base is its newest
        bases.setdefault(doc_store.base_stem(f), []).append(f)
    for f in reversed(files):
        for cand in (f, Path(f).stem, doc_store.base_stem(f)):
            c = _norm_name(cand)
            if len(c) >= 4 and c in q:
                return bases[doc_store.base_stem(f)][-1]
    words = {b: {w for w in _norm_name(b).split() if len(w) >= 4} for b in bases}
    qwords = set(q.split())
    for b, ws in words.items():
        others = set().union(*[v for k, v in words.items() if k != b]) if len(words) > 1 else set()
        if (ws - others) & qwords:
            return bases[b][-1]
    for rx, exts in _TYPE_WORDS:
        if re.search(r"\b(?:" + rx + r")\b", q):
            same = [f for f in files if Path(f).suffix.lower() in exts]
            if same:
                if len(same) > 1 and re.search(r"\b(previous|earlier|older|before|other|first)\b", q):
                    return same[-2] if "first" not in q else same[0]
                return same[-1]
    return files[-1]


def missing_type(query: str, files: list) -> str:
    """'spreadsheet' | 'pdf' | 'document' | 'presentation' when the message names a kind of file that this chat has not produced
    yet ("where is the spreadsheet?" after only a .md came back), else ''. The old answer re-sent the newest file of ANY type."""
    q = _norm_name(query)
    label = {".pdf": "pdf", ".xlsx": "spreadsheet", ".docx": "document", ".pptx": "presentation"}
    for rx, exts in _TYPE_WORDS:
        if re.search(r"\b(?:" + rx + r")\b", q):
            if not any(Path(f).suffix.lower() in exts for f in files):
                return label[exts[0]]
            return ""
    return ""


_SHEETISH_RE = re.compile(r"\b(sheets?|tabs?|columns?|rows?|cells?|headers?|formulas?|totals?|charts?|workbook|spreadsheet|excel|xlsx)\b", re.I)
_DECKISH_RE = re.compile(r"\b(images?|pictures?|photos?|graphs?|charts?|infographics?|slides?|visuals?|theme|colou?rs?|fonts?|cover|title slide|"
                         r"bullets?|speaker notes?|layout|design)\b", re.I)
_NEW_FILE_RE = re.compile(r"\b(new|another|separate|second|different|fresh|brand new)\b.{0,25}\b(sheet|workbook|spreadsheet|excel|file|xlsx)\b", re.I)


def file_followup_intent(query: str, has_prior: bool, prior_files=None, asked: bool = False) -> Optional[str]:
    """'where' = user can't find / wants the existing file again;
    'edit' = user wants the existing file changed. None otherwise."""
    q = (query or "").strip()
    if not has_prior or not q:
        return None
    if _FILE_WHERE_RE.search(q) and not _FILE_EDIT_RE.search(q):
        return "where"
    has_deck = any(str(f).lower().endswith((".pptx", ".ppt")) for f in (prior_files or []))
    if has_deck and not _NEW_FILE_RE.search(q) and _DECKISH_RE.search(q):
        return "edit"                      # "where are the images, graphs?" after a deck: the deck already in this chat, not a new one
    has_book = any(str(f).lower().endswith((".xlsx", ".xls")) for f in (prior_files or []))
    if has_book and not _NEW_FILE_RE.search(q):
        if _SHEETISH_RE.search(q):
            return "edit"                  # "the cost sheet needs a Month column": the workbook already in this chat, not a new one
        if asked and len(q) < 500:
            return "edit"                  # the user is answering a question the assistant asked while working on it
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


KB_TOOL_NAMES = ("search_knowledge_base", "kb_find_mentions", "kb_query", "kb_sql")


CHAT_CUT_CALL_ERROR = (
    "error: this write_file call was cut off by the output limit (its arguments were not complete JSON), so nothing was saved. "
    "Write a smaller file: one file per call, and keep each under about 250 lines. For a website, save a compact index.html "
    "(CSS and JS inline or in separate short files) instead of one huge page, then give the [DOWNLOAD: ...] link.")


def serves_brief_card(content: str, brief_phase: str) -> bool:
    """True when the reply is (or must become) the "few quick choices" card, so file-intent recovery must not save it as a file."""
    return brief_phase == "ask" or "```questionnaire" in (content or "")


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


def shrink_old_tool_results(msgs: list, budget_tokens: int, keep_last: int = 2, head_chars: int = 800,
                            tools=None) -> None:
    """Research loops pile up 6-20 KB web results per call. Once the prompt
    passes the budget, cut older tool results down to their head (the latest
    `keep_last` stay whole), then fall back to digest compaction.
    tools are the schemas sent with the next call: every estimate here must see
    them, or the schema block (thousands of tokens) is invisible to the budget."""
    if estimate_prompt_tokens(msgs, tools) <= budget_tokens:
        return
    tool_idx = [i for i, m in enumerate(msgs) if m.get("role") == "tool"]
    for i in tool_idx[:-keep_last] if keep_last else tool_idx:
        c = str(msgs[i].get("content") or "")
        if len(c) > head_chars:
            msgs[i]["content"] = c[:head_chars] + f"\n...[{len(c) - head_chars} chars trimmed to fit context]"
    if estimate_prompt_tokens(msgs, tools) > budget_tokens:
        msgs[:] = compact_messages(msgs, budget_tokens, tools)
