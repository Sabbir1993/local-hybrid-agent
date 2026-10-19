import re
from collections import Counter


def is_degeneration_or_loop(text: str) -> tuple[bool, str]:
    if not text or len(text) < 70:
        return False, text

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) >= 3:
        for i in range(len(lines) - 2):
            if lines[i] == lines[i+1] == lines[i+2]:
                cut_idx = text.find(lines[i+1])
                if cut_idx != -1:
                    return True, text[:cut_idx].rstrip()
                return True, text

    sentences = [s.strip() for s in re.split(r'[.\n]+', text) if len(s.strip()) > 20]
    if len(sentences) >= 3:
        counts = Counter(sentences)
        for s, count in counts.items():
            if count >= 3:
                first_pos = text.find(s)
                second_pos = text.find(s, first_pos + len(s))
                if second_pos != -1:
                    return True, text[:second_pos].rstrip()
                return True, text

    for phrase_len in (25, 35, 50, 70):
        if len(text) > phrase_len * 3:
            phrase = text[-phrase_len:]
            if text.count(phrase) >= 3:
                first_pos = text.find(phrase)
                second_pos = text.find(phrase, first_pos + phrase_len)
                if second_pos != -1:
                    return True, text[:second_pos].rstrip()
                return True, text

    return False, text


def is_repeating_loop(text: str) -> bool:
    loop_detected, _ = is_degeneration_or_loop(text)
    return loop_detected


def sanitize_user_facing_content(text: str) -> str:
    if not text:
        return ""
    is_loop, cleaned = is_degeneration_or_loop(text)
    text = cleaned if is_loop else text
    text = re.sub(r"<tool_call>[\s\S]*?</tool_call>", "", text)
    text = re.sub(r'<function\s+name=["\'][\s\S]*?</function>', "", text)
    text = re.sub(r"</?(?:tool_call|function|param|tool_response|tool_sep|im_start|im_end)>", "", text)
    text = text.replace("<![CDATA[", "").replace("]]>", "")
    text = re.sub(r"```(?:json)?\s*\{\s*\"name\"\s*:\s*\"(?:write_file|edit_file|read_file|list_files|run_python)\"[\s\S]*?\}\s*```", "", text)
    return text.strip()


_SEP_LINE = re.compile(r"^[\s\-=_*#|:+.~`>]*$")
RUNAWAY_MSG = ("error: your output started repeating itself and was cut off. Write it again shorter, "
               "without repeating phrases, lines or URLs. For a long file write only the first section now and add the rest "
               "in later project_append calls")
_MARKUP_TAIL = re.compile(r"https?://|<[a-zA-Z/!]|=(?:\"|\\\"|')[^\"'\\]*(?:\"|\\\"|')|[{};]\s*$|\bclass=|\bsrc=|\bhref=|<\/?(?:div|tr|td|th|li|span|p|table|tbody|thead)\b")


def _table_like(lines: list) -> bool:
    """Most non-empty lines are table rows (pipes, tabs, html rows) or csv rows: repeated cell values there are data, not a loop."""
    body = [ln for ln in lines if ln.strip()]
    if len(body) < 4:
        return False
    rows = sum(1 for ln in body if ln.count("|") >= 2 or ln.count("\t") >= 2 or ln.count('","') >= 2 or ln.count(",") >= 4 or "<td" in ln or "<tr" in ln or "<th" in ln)
    return rows >= 0.3 * len(body)


def find_runaway(text: str, window: int = 6000) -> int:
    """Index to cut `text` at when its tail has clearly started to loop, else -1.

    Stricter than is_degeneration_or_loop (which also fires on a phrase seen three times) so that real
    outlines, tables and slide lists never trip it: a 40+ character phrase with real words that occurs six
    times inside the last `window` characters, or six identical consecutive non-separator lines. The cut
    keeps one copy of the repeated text."""
    if not text or len(text) < 400:
        return -1
    base = max(0, len(text) - window)
    tail = text[base:]
    lines = tail.split("\n")
    run, pos = 1, 0
    starts = []
    for ln in lines:
        starts.append(pos)
        pos += len(ln) + 1
    for i in range(1, len(lines)):
        cur = lines[i].strip()
        if cur and len(cur) >= 8 and cur == lines[i - 1].strip() and not _SEP_LINE.match(cur):
            run += 1
            if run >= 6:
                return base + starts[i - run + 2]       # keep the first copy
        else:
            run = 1
    if _table_like(lines):
        return -1       # rows of a table / csv legitimately share long cell values ("| Medium | Core App Dev | High |" x 12)
    for plen in (40, 70, 120, 200):
        if len(tail) < plen * 6:
            continue
        phrase = tail[-plen:]
        if len(re.findall(r"[A-Za-zঀ-৿0-9]{2,}", phrase)) < 5:
            continue
        if _MARKUP_TAIL.search(phrase):
            continue        # a shared tail of image links / tag attributes (cards, gallery) is markup, not a loop: identical lines still count above
        if tail.count(phrase) >= (8 if plen < 70 else 6):
            first = tail.find(phrase)
            return base + first + plen                   # keep the first copy
    return -1
