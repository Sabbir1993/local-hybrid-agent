"""Pure text-editing helpers behind read_file / edit_file / append_file / insert_at_line.

They work on strings only (no disk, no companion), so the rules are unit-testable and the
same for every device. Files are handled with "\\n" line endings internally and converted back
to the file's own ending (CRLF on Windows) when the result is written.

E-edit matching tiers for old/new edits (attempted in order, all loud on failure):
  1. exact substring
  2. whitespace/indentation-normalized block
  3. fuzzy block: >= 2-line blocks with similarity >= FUZZY_THRESHOLD at exactly
     ONE location (content drift - the dominant edit_file failure mode)
  4. unified diff input via apply_diff (models write diffs more reliably than
     they copy blocks); hunks are verified against the file and applied atomically
"""
import difflib
import re
from typing import Optional

# "   12\t..." (read_file output) or "12→..." (cat -n style): display only, never file content
_PREFIX_RX = re.compile(r"^ *\d+[\t→]")
SNIPPET_CONTEXT = 3
SNIPPET_MAX_LINES = 30
CANDIDATES = 3
# Tier-3 gate: a drifted block must still be recognizably the same code. Below
# this, "close" lines are treated as not-found (the close-candidates hint is
# the response). 0.85 keeps typos/reworded comments in, different logic out.
FUZZY_THRESHOLD = 0.85
# Blocks shorter than this have no structure to be fuzzy about: one drifted
# line matching at 0.9 could be a genuinely different line. Exact/whitespace
# tiers still handle them.
FUZZY_MIN_LINES = 2
_HUNK_RX = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


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


def _find_similar(file_lines: list, old_lines: list) -> list:
    """Tier-3 candidates: [(start_index, score)] where the mean per-line
    SequenceMatcher ratio against the whitespace-normalized block clears
    FUZZY_THRESHOLD. Window equals the block length - no reordering, so a
    match still has the same shape as what the model copied."""
    old = [ln for ln in old_lines if ln.strip()]
    if len(old) < FUZZY_MIN_LINES:
        return []
    n = len(old)
    norm_file = [_norm_ws(x) for x in file_lines]
    norm_old = [_norm_ws(x) for x in old]
    hits = []
    for i in range(0, len(file_lines) - n + 1):
        total = 0.0
        for k in range(n):
            f, o = norm_file[i + k], norm_old[k]
            if f == o:
                total += 1.0
            else:
                total += difflib.SequenceMatcher(None, f, o).ratio()
        score = total / n
        if score >= FUZZY_THRESHOLD:
            hits.append((i, score))
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


def _apply_block(src_lines: list, old_lines: list, new: str, start: int) -> tuple[str, int, int]:
    """Replace src_lines[start:start+len(old_lines)] with `new`, reindented from the
    old block's first non-empty line to the file's actual first-line indent."""
    trimmed = list(old_lines)
    while trimmed and not trimmed[0].strip():
        trimmed.pop(0)
    while trimmed and not trimmed[-1].strip():
        trimmed.pop()
    n = len(trimmed)
    new_txt = _reindent(new.strip("\n") if new.strip() else new,
                        _indent(trimmed[0]), _indent(src_lines[start]))
    src_lines[start:start + n] = new_txt.split("\n") if new_txt != "" else []
    out = "\n".join(src_lines)
    first = start + 1
    return out, first, first + max(new_txt.count("\n"), 0)


def apply_edit(text: str, old: str, new: str, replace_all: bool = False) -> dict:
    """Replace `old` with `new` in `text`, tiered: exact -> whitespace -> fuzzy.

    Returns {text, count, method, first_line, last_line, snippet}. Raises EditError with a
    message that tells the model what to do next.
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
        # Tier 2: whitespace/indentation tolerance (exact modulo whitespace runs).
        hits = _find_fuzzy(src_lines, old_lines)
        if len(hits) == 1:
            out, first, last = _apply_block(src_lines, old_lines, new, hits[0])
            return {"text": from_lf(out, eol), "count": 1, "method": "whitespace",
                    "first_line": first, "last_line": last, "snippet": snippet(out, first, last)}
        if len(hits) > 1:
            where = ", ".join(str(h + 1) for h in hits[:5])
            raise EditError(f"old_string matches {len(hits)} places once whitespace is ignored "
                            f"(lines {where}) - include more surrounding lines to make it unique")
        # Tier 3: content drift. The block is almost right - apply at the one
        # location that is recognizably the same code, or stay loud.
        sim = _find_similar(src_lines, old_lines)
        if len(sim) == 1:
            out, first, last = _apply_block(src_lines, old_lines, new, sim[0][0])
            return {"text": from_lf(out, eol), "count": 1, "method": "fuzzy",
                    "first_line": first, "last_line": last, "snippet": snippet(out, first, last)}
        if len(sim) > 1:
            where = ", ".join(str(h + 1) for h, _s in sim[:5])
            raise EditError(f"old_string is close to the text at {len(sim)} places "
                            f"(lines {where}) - the block drifted or is ambiguous; include "
                            "more surrounding lines so the target is unique")
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


def _parse_hunks(diff: str) -> list:
    """Parse a unified diff into hunks: [{old_start, old_count, old, new}] where old/new are
    plain content lines. Tolerant of the model's usual shortcuts: missing ---/+++ headers,
    missing line counts, and missing leading space on context lines. Loud about everything
    else - a malformed hunk fails the whole diff before anything is applied."""
    diff, _ = strip_line_prefixes(diff)
    diff = to_lf(diff)
    if diff.endswith("\n"):
        diff = diff[:-1]
    hunks, cur = [], None
    for ln in diff.split("\n") if diff else []:
        m = _HUNK_RX.match(ln)
        if m:
            if cur:
                hunks.append(cur)
            cur = {"old_start": int(m.group(1)), "old_count": 1 if m.group(2) is None else int(m.group(2)),
                   "old": [], "new": []}
            continue
        if cur is None:
            continue  # preamble: ---/+++/diff --git/index lines before the first hunk
        if ln.startswith("\\"):
            continue      # "\ No newline at end of file"
        if ln.startswith("+"):
            cur["new"].append(ln[1:])
        elif ln.startswith("-"):
            cur["old"].append(ln[1:])
        elif ln.startswith(" ") or ln == "":
            body = ln[1:] if ln else ""
            cur["old"].append(body)
            cur["new"].append(body)
        else:
            raise EditError(f"diff line {ln[:60]!r} is not part of a unified diff - "
                            "send standard @@ -a,b +c,d @@ hunks with -/+ lines")
    if cur:
        hunks.append(cur)
    if not hunks:
        raise EditError("no hunks found in diff - send a unified diff (@@ hunks with -/+ lines), "
                        "or use old_string/new_string instead")
    return hunks


def _locate(old_block: list, file_lines: list, declared_start: int, hunk_no: int) -> tuple[int, bool]:
    """Where the hunk's old lines live in the file: (start_index, fuzzy_match).

    Order: exact at the declared line, then exact anywhere (line drift), then
    whitespace-normalized anywhere. CONTENT drift is refused - a diff whose
    context no longer matches means the model's view is stale, and the honest
    response is re-read + regenerate, never fuzzy-application.
    """
    n = len(old_block)
    total = len(file_lines)
    declared = declared_start - 1
    if 0 <= declared <= total - n and file_lines[declared:declared + n] == old_block:
        return declared, False
    exact = [i for i in range(total - n + 1) if file_lines[i:i + n] == old_block]
    if len(exact) == 1:
        return exact[0], False
    if len(exact) > 1:
        where = ", ".join(str(i + 1) for i in exact[:5])
        raise EditError(f"hunk {hunk_no}: its lines match {len(exact)} places (lines {where}) - "
                        "add surrounding context lines to pin the location")
    want = [_norm_ws(x) for x in old_block]
    ws = [i for i in range(total - n + 1)
          if all(_norm_ws(file_lines[i + k]) == want[k] for k in range(n))]
    if len(ws) == 1:
        return ws[0], True
    if len(ws) > 1:
        where = ", ".join(str(i + 1) for i in ws[:5])
        raise EditError(f"hunk {hunk_no}: its lines match {len(ws)} places once whitespace is "
                        f"ignored (lines {where}) - add context lines to pin the location")
    return -1, False


def apply_diff(text: str, diff: str) -> dict:
    """Apply a unified diff (diff -u / git patch) to `text`, atomically.

    Every hunk is located and verified against the file BEFORE anything is
    applied; any failure (drifted context, ambiguity, garbage hunk) raises
    EditError and the text is untouched. Returns {text, hunks, method,
    first_line, last_line, snippet}.
    """
    if not diff or not diff.strip():
        raise EditError("diff is empty")
    eol = detect_eol(text)
    src = to_lf(text)
    file_lines = src.split("\n")
    trailing = src.endswith("\n")
    if trailing:
        file_lines.pop()                  # the newline that ends the file is not a line
    total = len(file_lines) if src else 0

    hunks = _parse_hunks(diff)
    edits = []                            # (start_index, old_len, new_lines)
    for hno, h in enumerate(hunks, 1):
        old_block, new_block = h["old"], h["new"]
        if h["old_count"] == 0:
            if old_block:
                raise EditError(f"hunk {hno}: header says it removes nothing but the hunk "
                                "contains - lines - regenerate it with correct counts")
            if not new_block:
                raise EditError(f"hunk {hno}: adds nothing (no + lines) - nothing to apply")
            start = h["old_start"]        # insert BEFORE this original line (unified diff: -L,0)
            if start < 1 or start > total + 1:
                raise EditError(f"hunk {hno}: inserts at line {start} but the file has "
                                f"{total} lines (use 1..{total + 1})")
            edits.append((start - 1, 0, list(new_block)))
            continue
        if not old_block:
            raise EditError(f"hunk {hno}: header declares {h['old_count']} old line(s) but the "
                            "hunk has no context or - lines - include the context to locate it")
        start, fuzzy = _locate(old_block, file_lines, h["old_start"], hno)
        if start < 0:
            probe = next((ln for ln in old_block if ln.strip()), "")
            cand = closest_lines(src, probe) if probe else []
            hint = (" Closest lines: " + "; ".join(f"line {ln}: {t[:80]!r}" for ln, t in cand)) if cand else ""
            raise EditError(f"hunk {hno} drifted: its context lines no longer match the file "
                            f"(expected around line {h['old_start']}).{hint} re-read the file "
                            "and regenerate the diff")
        if fuzzy:
            first_nonempty = next((ln for ln in old_block if ln.strip()), "")
            new_txt = _reindent("\n".join(new_block), _indent(first_nonempty),
                                _indent(file_lines[start]))
            new_lines = new_txt.split("\n") if new_txt != "" else []
        else:
            new_lines = list(new_block)
        edits.append((start, len(old_block), new_lines))

    edits.sort(key=lambda e: e[0])
    for a, b in zip(edits, edits[1:]):
        if b[0] < a[0] + a[1]:
            raise EditError("hunks overlap - the diff modifies the same lines twice; regenerate it")
    for start, n, new_lines in reversed(edits):
        file_lines[start:start + n] = new_lines
    out = "\n".join(file_lines) + ("\n" if trailing or not src else "")
    first = edits[0][0] + 1
    last = first + max(len(edits[0][2]) - edits[0][1], 0) + max(edits[0][1] - len(edits[0][2]), 0)
    last = max(last, first)
    return {"text": from_lf(out, eol), "hunks": len(hunks), "method": "diff",
            "first_line": first, "last_line": last, "snippet": snippet(out, first, last)}


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
