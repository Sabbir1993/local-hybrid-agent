"""Model text -> tables, the way people actually write them.

A local model rarely emits a perfect markdown table. What arrives is pipe-separated lines with no outer pipes
(`Module | Feature | Status`), several tables with a heading line above each, prose and bullets in between, csv whose
numbers carry thousands separators (`4,800,000/year` splits into three cells), and a first record where the header
should be. This module finds the tables, keeps each in its own sheet, keeps the surrounding text as an Overview / Notes
sheet instead of dropping it or pouring it into table cells, repairs ragged and over-split rows, and returns a workbook
outline (core.sheet_engine.build) so the result gets the standard styling, header, filter and number formats.

Delimiter choice is by evidence, not by order: pipes and tabs are rare in prose, so a run of lines with the same
count of them is a table; commas are the last resort because every sentence has one.
"""
import csv
import re
from typing import Optional

from .pseudo import with_header

MAX_SHEETS = 20
MAX_COLS = 40
_MD_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_CODE_LANGS = frozenset("python py javascript js jsx typescript ts tsx bash sh shell powershell ps1 zsh json xml html css yaml yml "
                        "toml ini c cpp h java go rust ruby php lua diff console terminal sql".split())
_THOUSANDS_HEAD = re.compile(r"^\D*\d{1,3}(?:,\d{3})*$")
_THOUSANDS_NEXT = re.compile(r"^\d{3}(?!\d)")


def _strip_fences(text: str) -> list:
    """Lines of `text` without ``` fence markers; fenced CODE blocks are dropped."""
    out, in_code = [], False
    for line in text.splitlines():
        m = re.match(r"^\s*```\s*([\w+-]*)\s*$", line)
        if m:
            if not in_code and m.group(1).lower() in _CODE_LANGS:
                in_code = True
            elif in_code:
                in_code = False
            continue
        if not in_code:
            out.append(line)
    return out


def _split_pipe(line: str) -> list:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.replace("\\|", "|").strip() for c in re.split(r"(?<!\\)\|", s)]


def _split_csv(line: str, delim: str) -> list:
    try:
        return [c.strip() for c in next(csv.reader([line], delimiter=delim))]
    except (csv.Error, StopIteration):
        return [c.strip() for c in line.split(delim)]


def _cells(line: str):
    """(delimiter, cells) for a line that looks like table data, else (None, None). Pipe and tab need to appear as real
    separators; comma and semicolon are tried by the caller only when the lines around agree."""
    if "|" in line:
        cells = _split_pipe(line)
        if len(cells) >= 2:
            return "|", cells
    if "\t" in line:
        cells = [c.strip() for c in line.split("\t")]
        if len(cells) >= 2 and sum(1 for c in cells if c) >= 2:
            return "\t", cells
    return None, None


def _merge_thousands(cells: list, want: int) -> list:
    """`['Cloud', '4', '800', '000/year']` -> `['Cloud', '4,800,000/year']` while the row has more cells than the table is wide."""
    out = list(cells)
    i = 0
    while len(out) > want and i < len(out) - 1:
        if _THOUSANDS_HEAD.match(out[i]) and _THOUSANDS_NEXT.match(out[i + 1]):
            out[i:i + 2] = [out[i] + "," + out[i + 1]]
            continue
        i += 1
    return out


def _fit(cells: list, width: int) -> list:
    cells = _merge_thousands(cells, width)
    if len(cells) > width:
        cells = cells[:width - 1] + [", ".join(c for c in cells[width - 1:] if c)]
    return cells + [""] * (width - len(cells))


def _title_of(line: str) -> str:
    t = re.sub(r"^[#>*\-\s]+|[*:\s]+$", "", line.strip())
    t = re.sub(r"^\d+[.)]\s*", "", t)
    return t if 2 <= len(t) <= 60 and "|" not in t else ""


def _blocks(lines: list) -> list:
    """[('table', title, rows, has_separator)] and [('text', lines)] in document order."""
    out, i, n = [], 0, len(lines)
    pending_title, text = "", []

    def flush_text():
        nonlocal text
        if any(t.strip() for t in text):
            out.append(("text", [t for t in text if t.strip()]))
        text = []

    while i < n:
        line = lines[i]
        delim, cells = _cells(line)
        if delim is None and _MD_SEP.match(line) and out and out[-1][0] == "table":
            i += 1
            continue
        if delim:
            run = []
            had_sep = False
            j = i
            while j < n:
                if _MD_SEP.match(lines[j]) and "-" in lines[j]:
                    had_sep = had_sep or bool(run)
                    j += 1
                    continue
                d2, c2 = _cells(lines[j])
                if d2 != delim:
                    break
                run.append(c2)
                j += 1
            if len(run) >= 2 or (len(run) == 1 and had_sep):
                width = len(run[0])                      # the first line (the header) sets the width; stray extras join the last cell
                if width < 2:
                    from collections import Counter
                    width = Counter(len(r) for r in run).most_common(1)[0][0]
                width = min(width, MAX_COLS)
                title = pending_title or ""
                if not title and text:
                    title = _title_of(text[-1])
                    if title:
                        text = text[:-1]
                flush_text()
                out.append(("table", title, [_fit(r, width) for r in run], had_sep))
                pending_title = ""
                i = j
                continue
        # comma / semicolon runs: only when two or more neighbouring lines split the same way and no pipe table is around
        if delim is None and line.strip():
            cd = next((d for d in (",", ";") if line.count(d) >= 1), None)
            if cd:
                run, j = [], i
                while j < n and lines[j].strip() and lines[j].count(cd) >= 1 and _cells(lines[j])[0] is None:
                    run.append(_split_csv(lines[j], cd))
                    j += 1
                from collections import Counter
                if len(run) >= 2:
                    modal = Counter(len(r) for r in run).most_common(1)[0][0]
                    close = [r for r in run if abs(len(r) - modal) <= 3 or _merge_thousands(r, modal) and len(_merge_thousands(r, modal)) == modal]
                    if modal >= 2 and len(close) >= max(2, int(len(run) * 0.7)):
                        title = ""
                        if text:
                            title = _title_of(text[-1])
                            if title:
                                text = text[:-1]
                        flush_text()
                        out.append(("table", title, [_fit(r, min(modal, MAX_COLS)) for r in run], False))
                        i = j
                        continue
        text.append(line)
        i += 1
    flush_text()
    return out


def to_outline(content: str) -> Optional[str]:
    """A workbook outline for text that contains at least one table, or None (nothing table-like: the caller falls back)."""
    lines = _strip_fences(str(content or ""))
    blocks = _blocks(lines)
    tables = [b for b in blocks if b[0] == "table"]
    if not tables:
        return None
    out, used, tcount = [], set(), 0
    single = len(tables) == 1 and not any(b[0] == "text" for b in blocks)

    def sheet_name(title: str, fallback: str) -> str:
        base = re.sub(r"[\[\]:*?/\\]", "-", title).strip()[:31] or fallback
        name, k = base, 2
        while name.lower() in used:
            name, k = f"{base[:28]} {k}", k + 1
        used.add(name.lower())
        return name

    def emit(name, rows):
        out.append(f"## Sheet: {name}")
        width = max(len(r) for r in rows)
        out.append("| " + " | ".join(_esc(h) for h in rows[0]) + " |")
        out.append("|" + "---|" * width)
        for r in rows[1:]:
            out.append("| " + " | ".join(_esc(c) for c in r) + " |")
        out.append("")

    first_table_at = next(i for i, b in enumerate(blocks) if b[0] == "table")
    for i, b in enumerate(blocks):
        if len(used) >= MAX_SHEETS:
            break
        if b[0] == "text":
            notes = [re.sub(r"^\s*(?:[-*\u2022]|\d+[.)])\s+", "", t).strip() for t in b[1]]
            notes = [re.sub(r"^#{1,6}\s*", "", t).replace("**", "") for t in notes if t]
            if len(notes) >= 1 and not single:
                name = sheet_name("Overview" if i < first_table_at else "Notes", "Notes")
                emit(name, [[name]] + [[t] for t in notes])
            continue
        _kind, title, rows, had_sep = b
        tcount += 1
        rows = rows if had_sep else with_header(rows)
        name = "Sheet1" if single else sheet_name(title, f"Table {tcount}")
        if single:
            used.add("sheet1")
        emit(name, rows)
    return "\n".join(out) if out else None


def _esc(cell) -> str:
    return str(cell).replace("|", "\\|").replace("\n", " ").strip()
