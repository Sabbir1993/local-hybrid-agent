"""Structure markers for knowledge-base text: sheets, pages, column headers, row numbers.

Extraction flattens spreadsheets and PDFs to text, and 900-character chunks then lose where a line came
from: a roster row ends up in a chunk without its column header, and no chunk knows its page or sheet.
This module writes small markers into the extracted text and copies the current context onto every chunk,
so a retrieved chunk is readable on its own and can be cited (page / sheet / row).

Markers, each on its own line:
    [Sheet: Name]          start of a worksheet
    Columns: a | b | c     the header row, repeated every HEADER_EVERY rows
    R12: v | v | v         a data row, with its spreadsheet row number
    [Page 7]               start of a PDF page
"""

import re
from typing import Iterable, Optional

HEADER_EVERY = 12            # repeat the column header this often, so each ~900-char chunk carries one
MAX_CELL_CHARS = 400

SHEET_RX = re.compile(r"^\[Sheet: (.+)\]\s*$", re.MULTILINE)
PAGE_RX = re.compile(r"^\[Page (\d+)\]\s*$", re.MULTILINE)
COLS_RX = re.compile(r"^Columns: (.+)$", re.MULTILINE)
ROW_RX = re.compile(r"^R(\d+): ", re.MULTILINE)


def _cell(c) -> str:
    if c is None:
        return ""
    s = " ".join(str(c).split())
    return s[:MAX_CELL_CHARS]


def looks_like_header(cells: list) -> bool:
    """A header row: two or more filled cells and none of them a plain number."""
    filled = [c for c in cells if c]
    if len(filled) < 2:
        return False
    return not any(re.fullmatch(r"[-+]?[\d,.]+%?", c) for c in filled)


def rows_to_text(rows: Iterable, sheet: Optional[str] = None) -> str:
    """Rows (iterables of cells, row 1 first) -> marker text. Empty cells stay empty so columns keep their place."""
    out = []
    if sheet is not None:
        out.append(f"[Sheet: {sheet}]")
    header = None
    since_header = 0
    for n, row in enumerate(rows, start=1):
        cells = [_cell(c) for c in row]
        while cells and not cells[-1]:
            cells.pop()
        if not cells:
            continue
        if header is None and n <= 5 and looks_like_header(cells):
            header = " | ".join(cells)
            out.append(f"Columns: {header}")
            since_header = 0
            continue
        if header is not None and since_header >= HEADER_EVERY:
            out.append(f"Columns: {header}")
            since_header = 0
        out.append(f"R{n}: " + " | ".join(cells))
        since_header += 1
    return "\n".join(out)


def page_marker(n: int) -> str:
    return f"[Page {n}]"


def _last(rx, text: str):
    m = None
    for m in rx.finditer(text):
        pass
    return m


def add_context(chunks: list) -> list:
    """Prefix each chunk with the sheet / page / columns in force where it starts, when the chunk lacks them.

    Chunks must be in document order. A chunk that already carries a marker keeps it; the context passed on
    to the next chunk is whatever was last seen in this one."""
    sheet = page = cols = None
    out = []
    for text in chunks:
        head = text.lstrip()[:200]                    # a marker that opens the chunk already gives its context
        prefix = []
        if sheet and not head.startswith("[Sheet:"):
            prefix.append(f"[Sheet: {sheet}]")
        if cols and not head.startswith("Columns:") and not head.startswith("[Sheet:"):
            prefix.append(f"Columns: {cols}")
        if page and not head.startswith("[Page "):
            prefix.append(f"[Page {page}]")
        out.append(("\n".join(prefix) + "\n" + text) if prefix else text)
        m = _last(SHEET_RX, text)
        if m:
            sheet, cols = m.group(1), None            # a new sheet has its own header
        m = _last(COLS_RX, text)
        if m:
            cols = m.group(1)
        m = _last(PAGE_RX, text)
        if m:
            page = m.group(1)
    return out


def location_after(text: str, loc: dict) -> dict:
    """Update {"sheet", "page", "cols"} with the last markers in `text` (used when walking chunks in order)."""
    m = _last(SHEET_RX, text)
    if m:
        loc["sheet"], loc["cols"] = m.group(1), None
    m = _last(COLS_RX, text)
    if m:
        loc["cols"] = m.group(1)
    m = _last(PAGE_RX, text)
    if m:
        loc["page"] = m.group(1)
    return loc
