"""Models often "make several sheets" by writing ONE csv-ish text with section lines such as `Sheet2,Cost Analysis` between
blocks. Saved as a flat table that is a single sheet with the headings as ordinary rows. This turns such text into a real
multi-sheet outline (core.sheet_engine.build) so the workbook has the sheets the author meant.
"""
import csv
import re
from typing import Optional

_MARK = re.compile(r"^\s*(?:#{1,4}\s*|={2,}\s*)?sheet\s*(\d+)\s*(?:[,:\-\u2013\u2014|]\s*(.*?))?\s*(?:={2,})?\s*$", re.I)
MAX_COLS = 30


def _escape(cell: str) -> str:
    return str(cell).replace("|", "\\|").replace("\n", " ").strip()


def to_outline(text: str) -> Optional[str]:
    """Outline text for a pseudo multi-sheet content, or None when the text does not look like one."""
    lines = str(text or "").splitlines()
    marks = [(i, int(m.group(1)), (m.group(2) or "").strip()) for i, ln in enumerate(lines) if (m := _MARK.match(ln))]
    if len(marks) < 2:
        return None
    nums = [n for _i, n, _t in marks]
    if nums != sorted(nums) or len(set(nums)) != len(nums):
        return None
    blocks = []
    for k, (i, n, title) in enumerate(marks):
        end = marks[k + 1][0] if k + 1 < len(marks) else len(lines)
        body = [ln for ln in lines[i + 1:end] if ln.strip()]
        if body:
            blocks.append((n, title, body))
    if len(blocks) < 2:
        return None
    pre = [ln for ln in lines[:marks[0][0]] if ln.strip()]
    out = []
    if pre and len(pre) == 1:
        out.append("# " + pre[0].strip("# ").strip()[:100])
    used = set()
    for n, title, body in blocks:
        rows = [r for r in csv.reader(body) if any(c.strip() for c in r)]
        if not rows:
            continue
        rows = with_header(rows)
        width = min(max(len(r) for r in rows), MAX_COLS)
        header = [h.strip() or f"Column{j + 1}" for j, h in enumerate((rows[0] + [""] * width)[:width])]
        name = re.sub(r"[\[\]:*?/\\]", "-", title)[:31].strip() or f"Sheet{n}"
        base, k = name, 2
        while name.lower() in used:
            name, k = f"{base[:28]} {k}", k + 1
        used.add(name.lower())
        out.append(f"## Sheet: {name}")
        out.append("| " + " | ".join(_escape(h) for h in header) + " |")
        out.append("|" + "---|" * width)
        for r in rows[1:]:
            cells = (r + [""] * width)[:width] if len(r) <= width else r[:width - 1] + [", ".join(r[width - 1:])]
            out.append("| " + " | ".join(_escape(c) for c in cells) + " |")
        out.append("")
    return "\n".join(out) if len([o for o in out if o.startswith("## Sheet:")]) >= 2 else None


_NUMERIC = re.compile(r"^[-+]?[\d,]*\.?\d+%?$")


def looks_like_header(rows: list) -> bool:
    """Is the first row a header (short labels), or already data? A model that forgets the header row makes its first record the
    header, which then shows bold with a sentence in it."""
    if len(rows) < 2:
        return True
    first = [str(c).strip() for c in rows[0]]
    filled = [c for c in first if c]
    if not filled:
        return False
    if any(len(c) > 45 or len(c.split()) > 6 for c in filled):
        return False
    rest = rows[1:]
    repeats = 0
    for ci, c in enumerate(first):
        col = [str(r[ci]).strip() for r in rest if ci < len(r) and str(r[ci]).strip()]
        if not c or not col:
            continue
        if _NUMERIC.match(c.replace(" ", "")):
            return False                                # a number is not a column label
        if col.count(c) >= 2:
            repeats += 1                                # "Critical" repeating down its own column is data, not a label
    return repeats < 2 and not (repeats and len(filled) <= 2)


def with_header(rows: list) -> list:
    """rows, with generic column names inserted on top when the first row is really data."""
    if not rows or looks_like_header(rows):
        return rows
    width = max(len(r) for r in rows)
    return [[f"Column{i + 1}" for i in range(width)]] + rows
