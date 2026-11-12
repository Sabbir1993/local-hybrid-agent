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

from . import pan

HEADER_EVERY = 12            # repeat the column header this often, so each ~900-char chunk carries one
MAX_CELL_CHARS = 400

SHEET_RX = re.compile(r"^\[Sheet: (.+)\]\s*$", re.MULTILINE)
PAGE_RX = re.compile(r"^\[Page (\d+)\]\s*$", re.MULTILINE)
COLS_RX = re.compile(r"^Columns: (.+)$", re.MULTILINE)
ROW_RX = re.compile(r"^R(\d+): ", re.MULTILINE)


def _cell(c) -> str:
    if c is None:
        return ""
    s = " ".join(str(c).split()).replace(" | ", " / ")      # " | " is the cell separator
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


# Screens for mask_text: they find the few table rows that can hold a card number, in one pass over the whole text, so the
# other rows (nearly all of them) cost no Python work. A screen only has to be a necessary condition; the exact check is
# pan.mask_cell / pan.mask_split_pan_cells on the hot row. Plain ASCII text (the usual case) gets the fast screens below;
# text with other digits (Bengali, fullwidth ...) goes through the same screens written for any digit.
def _screens(d: str) -> tuple:
    """(two neighbouring cells made of digits / spaces / dashes, with empty cells allowed between them: one regex for a cell
    after a separator, one for the first cell of a row) written for the digit class `d`."""
    q = f"[{d}][{d} \\-\u00a0]{{2,23}}"
    more = rf"(?=(?:[ \-|\u00a0]*[{d}]){{13}})"            # 13 digits ahead without leaving the digits-only cells: fewer is no card
    return (re.compile(rf"\| {more}{q}(?: \| ?)+[{d}]"), re.compile(rf"\nR[0-9]+: {more}{q}(?: \| ?)+[{d}]"))


_ASCII_SCREENS = _screens("0-9")
_ANY_SCREENS = _screens(pan._DIG)
# 12+ digits in a row, or a 4-digit group followed by a separator and a 3-digit group (any 1-6 separator characters), on the text
# with digits -> 9, letters -> a, "|" and newline kept, everything else -> " ": cell separators are not card separators
_CELL_PAN_ASCII = re.compile(rb"9999(?:9{8}| {1,6}999)")
_CELL_PAN_ANY = re.compile(rf"(?<![{pan._DIG}]){pan._D}{{4}}(?:{pan._D}|(?:(?!\|)[\W_]){{1,6}}{pan._D}){{9,}}")
_SQUASH = bytes(ord("9") if chr(c).isdigit() and c < 128 else ord("a") if chr(c).isalpha() and c < 128
                else c if chr(c) in "|\n" else 32 for c in range(256))
_CVV_HINT_RX = re.compile("cv[vc]|csc|cid|\u09b8\u09bf\u09ad\u09bf[\u09ad\u09b8]\u09bf")
_TABLE_START = re.compile(r"\s*(?:\[Sheet: |Columns: |R\d+: )")


def _hot_rows(text: str, single: bool) -> set:
    """Start offsets of the row lines that may hold a card number (see the screens above)."""
    hot = set()
    ascii_text = text.isascii()
    spans = [m.start() for rx in (_ASCII_SCREENS if ascii_text else _ANY_SCREENS) for m in rx.finditer(text)]
    if single:                                         # a card number inside one cell
        if ascii_text:
            squashed = text.encode("ascii").translate(_SQUASH)
            spans += [m.start() for m in _CELL_PAN_ASCII.finditer(squashed)]
        else:
            spans += [m.start() for m in _CELL_PAN_ANY.finditer(text)]
        if _CVV_HINT_RX.search(text.lower()):          # a CVV next to its keyword in a short cell (the 13-character rule skips those)
            spans += [m.start() for m in pan.CVV_RX.finditer(text)]
    for pos in spans:
        hot.add(text.rfind("\n", 0, pos + 1) + 1)
    if text.startswith("R"):                           # the first screen needs a newline before the row
        hot.add(0)
    return hot


_DONE_RX = re.compile(r"\[card (?:\*{4}\d{4}|part)\]")
_HOLD = "ZMASKZ"


def _mask_prose_once(text: str) -> str:
    if "[card " not in text or _HOLD in text:
        return pan.mask_pans(text)[0]
    kept = []
    held = _DONE_RX.sub(lambda m: kept.append(m.group(0)) or _HOLD, text)
    held = pan.mask_pans(held)[0]
    it = iter(kept)
    return re.sub(_HOLD, lambda m: next(it, ""), held)


def _mask_prose(text: str) -> str:
    """pan.mask_pans, repeated until the text stops changing, so that masking masked text changes nothing (re-running the
    masking over what is stored is safe). Two things make one pass not enough: the mask "[card ****1111]" contains the word
    "card", which counts as card context for a number nearby, and the shorter text brings that number closer to a card word it
    was just out of reach of. The masks are set aside while the text is scanned (so only real card words count), and a number
    that only now falls inside the context window is masked on the second pass. Text without a card number costs one pass."""
    for _ in range(3):
        out = _mask_prose_once(text)
        if out == text:
            break
        text = out
    return text


def mask_text(text: str, single: bool = True) -> str:
    """The text with card numbers masked, for storing (PCI DSS 3.5): what reaches the chunks, the full-text index, the embedder
    and the entity index is this text.

    Text that is a spreadsheet (rows_to_text markers, no page markers) is masked cell by cell: a plain pass over
    "R5: 4111 1111 | 1111 1111" would swallow the " | " between the cells and shift every column. Cells that together form a card
    number are masked as in pan.mask_split_pan_cells (always); a number inside one cell only when `single`. Other text (prose,
    PDF pages) goes through pan.mask_pans in one pass, so a number wrapped over two lines is still seen.
    Only a number that passes the card check is masked (Luhn + known prefix; or a card column / "card" nearby), so ids,
    phone numbers and account numbers stay as they are."""
    if not text:
        return text
    if not _TABLE_START.match(text) or "\n[Page " in text:
        return _mask_prose(text) if single else text
    from bisect import bisect_right
    hot = _hot_rows(text, single)
    edits = []
    if hot:
        sheets = [m.start() for m in SHEET_RX.finditer(text)] if single else []
        colss = [(m.start(), m.group(1)) for m in COLS_RX.finditer(text)] if single else []
        starts = [c[0] for c in colss]
        for a in sorted(hot):
            b = text.find("\n", a)
            b = len(text) if b < 0 else b
            line = text[a:b]
            m = ROW_RX.match(line)
            if not m:
                continue
            card_cols = []
            if single and colss:                       # the header in force at this row: the last one after the last sheet marker
                k, sk = bisect_right(starts, a) - 1, bisect_right(sheets, a) - 1
                if k >= 0 and (sk < 0 or sheets[sk] < colss[k][0]):
                    card_cols = [pan.is_card_header(c) for c in colss[k][1].split(" | ")]
            cells = line[m.end():].split(" | ")
            if single:
                cells = [pan.mask_cell(c, card_cols[i] if i < len(card_cols) else False) if len(c) >= 13
                         else (pan.mask_pans(c)[0] if pan.CVV_RX.search(c) else c) for i, c in enumerate(cells)]
            cells = pan.mask_split_pan_cells(cells)
            masked = line[:m.end()] + " | ".join(cells)
            if masked != line:
                edits.append((a, b, masked))
    if not edits:
        return text
    edits.sort()
    out, pos = [], 0
    for a, b, rep in edits:
        out.append(text[pos:a])
        out.append(rep)
        pos = b
    out.append(text[pos:])
    return "".join(out)
