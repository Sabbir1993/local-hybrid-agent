"""Pure text-editing helpers behind read_file / edit_file / append_file / insert_at_line.

They work on strings only (no disk, no companion), so the rules are unit-testable and the
same for every device. Files are handled with "\\n" line endings internally and converted back
to the file's own ending (CRLF on Windows) when the result is written.
"""
import difflib
import re
from typing import Optional

# "   12\t..." (read_file output) or "12→..." (cat -n style): display only, never file content
_PREFIX_RX = re.compile(r"^ *\d+[\t→]")
SNIPPET_CONTEXT = 3
SNIPPET_MAX_LINES = 30
CANDIDATES = 3


class EditError(ValueError):
    """The edit cannot be applied; the message is written for the model."""


def detect_eol(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def to_lf(text: str) -> str:
    return text.replace("\r\n", "\n")


def from_lf(text: str, eol: str) -> str:
    return text.replace("\n", eol) if eol == "\r\n" else text


def strip_line_prefixes(s: str) -> tuple[str, bool]:
    """Remove read_file's line-number prefixes from a pasted block. Only when EVERY non-empty
    line has one, so real content that merely starts with digits is left alone."""
    lines = s.split("\n")
    body = [ln for ln in lines if ln.strip()]
    if not body or not all(_PREFIX_RX.match(ln) for ln in body):
        return s, False
    return "\n".join(_PREFIX_RX.sub("", ln, count=1) if ln.strip() else ln for ln in lines), True


def numbered(text: str, offset: int = 1, limit: int = 200, max_chars: int = 20000) -> dict:
    """Lines offset..offset+limit-1 with "  N<TAB>" prefixes. The size cap can shorten the
    window; `next_offset` is where to continue, or None at the end of the file."""
    lines = to_lf(text).split("\n")
    if lines and lines[-1] == "" and len(lines) > 1:
        lines.pop()                       # the newline that ends the file is not a line
    total = len(lines) if text else 0
    offset = max(1, int(offset or 1))
    limit = max(1, int(limit or 200))
    out, used, i = [], 0, offset - 1
    while i < total and len(out) < limit:
        row = f"{i + 1:>6}\t{lines[i]}"
        if used + len(row) + 1 > max_chars and out:
            break
        if len(row) > max_chars:          # one enormous line: cut it, keep going
            row = row[:max_chars] + " ...[line cut]"
        out.append(row)
        used += len(row) + 1
        i += 1
    nxt = i + 1 if i < total else None
    return {"text": "\n".join(out), "first": offset if out else None,
            "last": offset + len(out) - 1 if out else None, "total": total, "next_offset": nxt}


def _norm_ws(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip())


def _indent(line: str) -> str:
    return line[:len(line) - len(line.lstrip(" \t"))]


def _find_fuzzy(file_lines: list, old_lines: list) -> list:
    """Start indexes where old_lines match file_lines ignoring indentation and whitespace runs."""
    while old_lines and not old_lines[0].strip():
        old_lines = old_lines[1:]
    while old_lines and not old_lines[-1].strip():
        old_lines = old_lines[:-1]
    if not old_lines:
        return []
    want = [_norm_ws(x) for x in old_lines]
    n = len(want)
    hits = []
    for i in range(0, len(file_lines) - n + 1):
        if _norm_ws(file_lines[i]) == want[0] and all(
                _norm_ws(file_lines[i + k]) == want[k] for k in range(1, n)):
            hits.append(i)
    return hits


def _reindent(new: str, old_first_indent: str, file_first_indent: str) -> str:
    if old_first_indent == file_first_indent:
        return new
    out = []
    for ln in new.split("\n"):
        if ln.strip() and ln.startswith(old_first_indent):
            ln = file_first_indent + ln[len(old_first_indent):]
        out.append(ln)
    return "\n".join(out)


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def closest_lines(text: str, old: str, n: int = CANDIDATES) -> list:
    """The file lines most like the first meaningful line of `old`: [(line_no, text)]."""
    probe = next((ln.strip() for ln in old.split("\n") if ln.strip()), "")
    if not probe:
        return []
    lines = text.split("\n")[:20000]
    stripped = {}
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s:
            stripped.setdefault(s, i + 1)
    best = difflib.get_close_matches(probe, list(stripped), n=n, cutoff=0.5)
    return sorted((stripped[b], b) for b in best)


def snippet(text: str, first_line: int, last_line: int) -> str:
    lines = text.split("\n")
    a = max(1, first_line - SNIPPET_CONTEXT)
    b = min(len(lines), last_line + SNIPPET_CONTEXT)
    if b - a + 1 > SNIPPET_MAX_LINES:
        b = a + SNIPPET_MAX_LINES - 1
    return "\n".join(f"{i:>6}\t{lines[i - 1]}" for i in range(a, b + 1))


def apply_edit(text: str, old: str, new: str, replace_all: bool = False) -> dict:
    """Exact replacement with a whitespace-tolerant fallback.

    Returns {text, count, method, first_line, last_line}. Raises EditError with a message that
    tells the model what to do next.
    """
    eol = detect_eol(text)
    src = to_lf(text)
    old, new = to_lf(old), to_lf(new)
    old, had_prefix = strip_line_prefixes(old)
    if had_prefix:
        new, _ = strip_line_prefixes(new)
    if not old:
        raise EditError("old_string is empty")
    if old == new:
        raise EditError("old_string and new_string are identical - nothing to change")

    count = src.count(old)
    method = "exact"
    if count == 0:
        src_lines = src.split("\n")
        old_lines = old.split("\n")
        hits = _find_fuzzy(src_lines, old_lines)
        if len(hits) == 1:
            i = hits[0]
            trimmed = [ln for ln in old_lines]
            while trimmed and not trimmed[0].strip():
                trimmed.pop(0)
            while trimmed and not trimmed[-1].strip():
                trimmed.pop()
            n = len(trimmed)
            new_txt = _reindent(new.strip("\n") if new.strip() else new,
                                _indent(trimmed[0]), _indent(src_lines[i]))
            src_lines[i:i + n] = new_txt.split("\n") if new_txt != "" else []
            out = "\n".join(src_lines)
            first = i + 1
            return {"text": from_lf(out, eol), "count": 1, "method": "whitespace",
                    "first_line": first, "last_line": first + max(new_txt.count("\n"), 0),
                    "snippet": snippet(out, first, first + new_txt.count("\n"))}
        if len(hits) > 1:
            where = ", ".join(str(h + 1) for h in hits[:5])
            raise EditError(f"old_string matches {len(hits)} places once whitespace is ignored "
                            f"(lines {where}) - include more surrounding lines to make it unique")
        cand = closest_lines(src, old)
        msg = "old_string not found in the file."
        if cand:
            msg += " Closest lines: " + "; ".join(f"line {ln}: {t[:100]!r}" for ln, t in cand)
        msg += " Use read_file on that range and copy the text exactly."
        raise EditError(msg)

    if count > 1 and not replace_all:
        pos, lines_at = 0, []
        while len(lines_at) < 5:
            j = src.find(old, pos)
            if j < 0:
                break
            lines_at.append(_line_of(src, j))
            pos = j + max(len(old), 1)
        raise EditError(f"old_string appears {count} times (lines {', '.join(map(str, lines_at))}) - "
                        "include more surrounding lines to make it unique, or set replace_all=true")

    first_idx = src.find(old)
    first_line = _line_of(src, first_idx)
    out = src.replace(old, new) if replace_all else src.replace(old, new, 1)
    last_line = first_line + new.count("\n")
    return {"text": from_lf(out, eol), "count": count if replace_all else 1, "method": method,
            "first_line": first_line, "last_line": last_line,
            "snippet": snippet(out, first_line, last_line)}


def append_text(text: Optional[str], chunk: str) -> str:
    """text + chunk with exactly one newline boundary between them."""
    if not text:
        return chunk
    eol = detect_eol(text)
    chunk = from_lf(to_lf(chunk), eol)
    if not text.endswith(("\n", "\r")) and not chunk.startswith(("\n", "\r")):
        return text + eol + chunk
    return text + chunk


def insert_at_line(text: str, line: int, block: str) -> dict:
    """Insert `block` so its first line becomes line number `line` (1 = top,
    total_lines+1 = end). Returns {text, first_line, last_line}."""
    eol = detect_eol(text)
    src = to_lf(text)
    lines = src.split("\n")
    trailing = src.endswith("\n")
    if trailing:
        lines.pop()
    total = len(lines) if src else 0
    try:
        line = int(line)
    except (TypeError, ValueError):
        raise EditError("line must be an integer")
    if line < 1 or line > total + 1:
        raise EditError(f"line {line} is outside the file (it has {total} lines; use 1..{total + 1})")
    ins = to_lf(block)
    if ins.endswith("\n"):
        ins = ins[:-1]
    new_lines = ins.split("\n")
    lines[line - 1:line - 1] = new_lines
    out = "\n".join(lines) + ("\n" if trailing or not src else "")
    return {"text": from_lf(out, eol), "first_line": line, "last_line": line + len(new_lines) - 1,
            "snippet": snippet(out, line, line + len(new_lines) - 1)}
