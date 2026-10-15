"""Compact profile of a spreadsheet (xlsx / xls / csv) for the model's context, and the table loader behind data_query.

A frontier harness does not paste a 20,000-row sheet into the prompt: it tells the model what the data IS (sheets, rows x
columns, column types, empties, ranges, the common values, a few sample rows) and lets it compute the rest. This module
builds that picture with openpyxl / xlrd / csv only (no pandas).
"""
import csv
import datetime as _dt
import re
from collections import Counter
from pathlib import Path
from typing import Optional

MAX_LOAD_ROWS = 100_000          # rows read per sheet (memory bound)
PROFILE_MAX_CHARS = 4000
SAMPLE_ROWS = 5
MAX_COLS_SHOWN = 40
_NUM_RX = re.compile(r"^[-+]?\(?\d[\d,]*\.?\d*\)?%?$")
_DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$|^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}$")


class Table:
    """One sheet: its header row, data rows (lists), and how many rows existed vs were loaded."""

    def __init__(self, name: str, headers: list, rows: list, total_rows: int):
        self.name, self.headers, self.rows, self.total_rows = name, headers, rows, total_rows
        self.preamble: list = []       # title / note rows found above the header
        self.formulas: dict = {}       # header -> an example formula, for columns that hold formulas (xlsx)

    @property
    def truncated(self) -> bool:
        return self.total_rows > len(self.rows)


def _clean_headers(cells: list) -> list:
    out, seen = [], Counter()
    for i, c in enumerate(cells):
        h = re.sub(r"\s+", " ", str(c)).strip() if c is not None else ""
        h = h or f"col{i + 1}"
        seen[h] += 1
        out.append(h if seen[h] == 1 else f"{h}_{seen[h]}")
    return out


def _is_blank(row) -> bool:
    return all(c is None or (isinstance(c, str) and not c.strip()) for c in row)


HEADER_SCAN = 30                 # non-blank rows looked at to find the header (titles / banners / notes sit above it)


def _filled(row) -> list:
    return [c for c in row if not (c is None or (isinstance(c, str) and not c.strip()))]


def header_index(buf: list) -> int:
    """Which of the first non-blank rows is the header. A sheet often opens with a title, a "prepared by" line or a merged
    banner (one filled cell); the header is the first row that is about as wide as the widest row and is all text."""
    widest = max((len(_filled(r)) for r in buf), default=0)
    if widest < 2:
        return 0
    need = max(2, -(-widest * 6 // 10))
    for i, r in enumerate(buf):
        cells = _filled(r)
        if len(cells) >= need and all(isinstance(c, str) for c in cells):
            return i
    return 0


def _table_from_rows(name: str, rows_iter, max_rows: int) -> Optional[Table]:
    buf, stream = [], iter(rows_iter)
    for row in stream:
        row = list(row)
        if _is_blank(row):
            continue
        buf.append(row)
        if len(buf) >= HEADER_SCAN:
            break
    if not buf:
        return None
    hi = header_index(buf)
    headers = _clean_headers(buf[hi])
    preamble = [" ".join(str(c).strip() for c in _filled(r))[:120] for r in buf[:hi]]
    rows, total = [], 0

    def take(row):
        nonlocal total
        total += 1
        if len(rows) < max_rows:
            rows.append(row)
    for row in buf[hi + 1:]:
        take(row)
    for row in stream:
        row = list(row)
        if not _is_blank(row):
            take(row)
    t = Table(name, headers, rows, total)
    t.preamble = preamble
    return t


def read_tables(path, max_rows: int = MAX_LOAD_ROWS) -> list:
    """All sheets of a workbook / the one table of a csv, as Table objects (header = first non-empty row)."""
    p = Path(path)
    suffix = p.suffix.lower()
    tables = []
    if suffix == ".csv":
        text = None
        for enc in ("utf-8-sig", "utf-16", "latin-1"):
            try:
                text = p.read_text(encoding=enc)
                break
            except (UnicodeError, OSError):
                continue
        if text is None:
            return []
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        t = _table_from_rows(p.stem, csv.reader(text.splitlines(), dialect), max_rows)
        return [t] if t else []
    if suffix == ".xls":
        import xlrd
        wb = xlrd.open_workbook(str(p))
        for sh in wb.sheets():
            rows = ([sh.cell_value(r, c) for c in range(sh.ncols)] for r in range(sh.nrows))
            t = _table_from_rows(sh.name, rows, max_rows)
            if t:
                tables.append(t)
        return tables
    import openpyxl
    wb = openpyxl.load_workbook(str(p), read_only=True, data_only=True)
    try:
        for ws in wb.worksheets:
            t = _table_from_rows(ws.title, ws.iter_rows(values_only=True), max_rows)
            if t:
                tables.append(t)
    finally:
        wb.close()
    _note_formulas(p, tables)
    return tables


FORMULA_SCAN_ROWS = 400


def _note_formulas(path, tables: list) -> None:
    """Which columns are formulas (first rows only): the data view shows cached values, and a workbook that was written by
    a script and never opened in Excel has none, so those columns would look empty. Sets table.formulas {header: example}."""
    try:
        import openpyxl
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=False)
    except Exception:
        return
    try:
        by_name = {t.name: t for t in tables}
        for ws in wb.worksheets:
            t = by_name.get(ws.title)
            if t is None:
                continue
            found = {}
            for n, row in enumerate(ws.iter_rows(values_only=True)):
                if n >= FORMULA_SCAN_ROWS:
                    break
                for ci, v in enumerate(row):
                    if isinstance(v, str) and v.startswith("=") and ci < len(t.headers) and t.headers[ci] not in found:
                        found[t.headers[ci]] = v[:60]
            t.formulas = found
    except Exception:
        pass
    finally:
        wb.close()


# ---- column typing -------------------------------------------------------------------------------------------------
def parse_number(v) -> Optional[float]:
    """A number from a cell: real numbers, and text like '1,200.50', '(300)', '12%'. None for anything else."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if s and _NUM_RX.match(s):
            neg = s.startswith("(") and s.endswith(")")
            try:
                n = float(s.strip("()%+").replace(",", ""))
            except ValueError:
                return None
            return -n if neg or s.startswith("-") and n > 0 else n
    return None


def _kind(v) -> str:
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (_dt.datetime, _dt.date)):
        return "date"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        s = v.strip()
        if _DATE_RX.match(s):
            return "date"
        if parse_number(s) is not None:
            return "number"
        return "text"
    return "text"


_ID_HEADER_RX = re.compile(r"(?i)(^|[^a-z])(id|no|nr|num|number|doc|document|code|ref|reference|account|acct|invoice|order|sku)([^a-z]|$)|#")
_AMOUNT_HEADER_RX = re.compile(r"(?i)amount|amt|price|total|value|cost|qty|quantity|balance|sum|rate|fee|charge|vat|tax|revenue|sales")


def column_stats(values: list, header: str = "") -> dict:
    """{kind, empty, distinct, min/max/mean/sum (numbers), min/max (dates), top (text)} for one column."""
    vals = [v for v in values if not (v is None or (isinstance(v, str) and not v.strip()))]
    out = {"empty": len(values) - len(vals), "count": len(vals)}
    if not vals:
        out["kind"] = "empty"
        return out
    kinds = Counter(_kind(v) for v in vals)
    kind = kinds.most_common(1)[0][0]
    out["kind"] = kind
    out["distinct"] = len({str(v).strip() for v in vals[:50000]})
    if kind == "number":
        nums = [n for n in (parse_number(v) for v in vals) if n is not None]
        if nums:
            out.update(min=min(nums), max=max(nums), sum=sum(nums), mean=sum(nums) / len(nums))
            # a column of all-different whole numbers that is NAMED like an identifier (document no., account no.): a sum or
            # mean means nothing. Judged by the header too, or every column of distinct whole amounts would lose its total.
            out["idlike"] = (len(nums) >= 10 and out["distinct"] == len(vals) and all(float(n).is_integer() for n in nums)
                             and bool(_ID_HEADER_RX.search(header)) and not _AMOUNT_HEADER_RX.search(header))
    elif kind == "date":
        as_text = sorted(str(v)[:10] for v in vals)
        out.update(min=as_text[0], max=as_text[-1])
    else:
        top = Counter(re.sub(r"\s+", " ", str(v)).strip() for v in vals).most_common(4)
        out["top"] = top
    return out


def _num(n: float) -> str:
    if abs(n) >= 1000 or float(n).is_integer():
        return f"{n:,.0f}" if float(n).is_integer() else f"{n:,.2f}"
    return f"{n:.3g}"


def _cell(v, width: int = 28) -> str:
    if isinstance(v, _dt.datetime) and v.hour == v.minute == v.second == 0:
        v = v.date()
    s = re.sub(r"\s+", " ", str(v)).strip() if v is not None else ""
    return s if len(s) <= width else s[: width - 1] + "…"


def _column_line(h: str, st: dict) -> str:
    kind = st["kind"]
    bits = [f"{kind}"]
    if st.get("empty"):
        bits.append(f"{st['empty']:,} empty")
    if kind == "number" and st.get("idlike"):
        bits[0] = "number (identifier)"
        bits.append(f"{st['distinct']:,} distinct, {_num(st['min'])} to {_num(st['max'])}")
    elif kind == "number" and "min" in st:
        bits.append(f"min {_num(st['min'])} | max {_num(st['max'])} | mean {_num(st['mean'])} | sum {_num(st['sum'])}")
    elif kind == "date" and "min" in st:
        bits.append(f"{st['min']} to {st['max']}")
    elif kind == "text" and st.get("top"):
        if st["distinct"] <= 12:
            bits.append(f"{st['distinct']} value{'s' if st['distinct'] != 1 else ''}: " + ", ".join(f"{_cell(v, 24)} {c:,}" for v, c in st["top"]))
        else:
            bits.append(f"{st['distinct']:,} distinct, e.g. " + ", ".join(f"\"{_cell(v, 24)}\"" for v, _ in st["top"][:3]))
    return f"- {_cell(h, 40)}: " + "; ".join(bits)


def profile_table(t: Table) -> str:
    lines = [f"## Sheet \"{t.name}\" - {t.total_rows:,} data rows x {len(t.headers)} columns"
             + (f" (first {len(t.rows):,} rows profiled)" if t.truncated else "")]
    cols = list(zip(*[(r + [None] * len(t.headers))[: len(t.headers)] for r in t.rows])) if t.rows else [[] for _ in t.headers]
    for h, values in list(zip(t.headers, cols))[:MAX_COLS_SHOWN]:
        line = _column_line(h, column_stats(list(values), h))
        if h in t.formulas:
            line += f"; formula column, e.g. {t.formulas[h]}"
            if line.startswith(f"- {_cell(h, 40)}: empty"):
                line += " (no calculated values stored: open and save it in Excel, or recompute from the other columns)"
        lines.append(line)
    if len(t.headers) > MAX_COLS_SHOWN:
        lines.append(f"- ... {len(t.headers) - MAX_COLS_SHOWN} more columns not shown")
    if t.preamble:
        lines.insert(1, "Title rows above the header (not data): " + " | ".join(t.preamble[:3]))
    if t.rows:
        lines.append(f"Sample rows (first {min(SAMPLE_ROWS, len(t.rows))}):")
        for i, r in enumerate(t.rows[:SAMPLE_ROWS], start=1):
            lines.append(f"  {i}: " + " | ".join(_cell(c) for c in r[: min(len(t.headers), 12)]))
    return "\n".join(lines)


def profile_file(path, max_chars: int = PROFILE_MAX_CHARS) -> tuple:
    """(profile text, complete?) for an xlsx / xls / csv. complete is False when rows, columns, sheets or characters
    were left out, so the caller can tell the model the rest is reachable with data_query / read_file_chunk."""
    p = Path(path)
    tables = read_tables(p)
    if not tables:
        return f"(no data found in {p.name})", True
    parts, complete = [f"Data file: {p.name} - {len(tables)} sheet{'s' if len(tables) != 1 else ''}, "
                       f"{sum(t.total_rows for t in tables):,} data rows in total"], True
    for t in tables:
        block = profile_table(t)
        if sum(len(x) + 1 for x in parts) + len(block) > max_chars:
            left = len(tables) - sum(1 for x in parts if x.startswith("## Sheet"))
            parts.append(f"... {left} more sheet{'s' if left != 1 else ''} not shown (use data_query with sheet=<name>)")
            complete = False
            break
        parts.append(block)
        complete = complete and not t.truncated and len(t.headers) <= MAX_COLS_SHOWN and t.total_rows <= SAMPLE_ROWS
    text = "\n\n".join(parts)
    if len(text) > max_chars:
        text, complete = text[:max_chars].rsplit("\n", 1)[0] + "\n...", False
    return text, complete
