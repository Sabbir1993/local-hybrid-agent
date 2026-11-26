"""Workbook builder: a short text outline -> a real, formatted, formula-driven .xlsx.

Outline (what the model writes):

    # Q3 Sales
    theme: corporate
    ## Sheet: Assumptions
    | Item | Value |
    |---|---|
    | Tax rate | 15% |
    name: TaxRate = Assumptions!B2
    ## Sheet: Sales
    | Date | Region | Revenue | Tax |
    |---|---|---|---|
    | 2026-07-05 | Dhaka | 100 | =C2*TaxRate |
    format: Revenue=currency:BDT, Tax=currency:BDT
    total: Revenue=sum, Tax=sum
    highlight: Revenue<100 red
    scale: Revenue
    validate: Region in [Dhaka, Khulna]
    chart: bar | title=Revenue by region | x=Region | y=Revenue
    link: Region                (cells that name a sheet of the workbook become links to it)
    rowformat: Net margin=0.0%  (one row's number cells get this format, over the column's: a wide period table mixes units)

A dashboard sheet has no table: KPI tiles (big numbers that point at the data with formulas) and charts over other sheets:

    ## Dashboard: Overview
    title: Q3 Sales Overview
    kpi: Total revenue = =SUM(Sales!C2:C200) | format=currency:BDT
    kpi: Orders = =COUNTA(Sales!A2:A200)
    kpi: Tax rate = =TaxRate | format=percent
    chart: bar | source=Sales | x=Region | y=Revenue | title=Revenue by region
    chart: line | source=Sales | x=Date | y=Revenue | title=Revenue over time
    note: Figures update when the Sales sheet changes.

A data sheet can carry a report layout above its table (all optional, per sheet):

    ## Sheet: Financials
    banner: 02 - Financial performance          (merged title row, accent fill)
    subtitle: Unit: USD millions, consolidated  (merged note row)
    | Metric | FY2024 | FY2025 |
    |---|---|---|
    | >> INCOME STATEMENT | | |                 (section band, merged across the table)
    | Revenue | 100 | 120 |
    input: FY2024, FY2025                       (yellow fill + blue text: cells the reader may change)

The header row is 1 without a banner, 3 with a banner only, 4 with banner + subtitle (a blank row separates the layout from the
table). Data row n sits at row `header + n`, so formulas written for such a sheet must use those rows.

Everything after the first table of a sheet that looks like `key: value` is a directive (see DIRECTIVES). Formulas are checked by
core.sheet_engine.formulas before they are written; anything refused raises BuildError with the cell and the reason.
"""
import contextvars
import io
import re
from typing import Optional

from .formulas import FormulaError, check_formula, to_file_form

MAX_SHEETS = 30
MAX_COLS = 100
MAX_ROWS_TOTAL = 50_000

DIRECTIVES = ("format", "total", "freeze", "filter", "highlight", "scale", "databar", "validate", "table", "name", "chart",
              "widths", "note", "print", "tab", "wrap", "hide", "merge", "color", "align", "border", "kpi", "title",
              "banner", "subtitle", "input", "icons", "style", "auto", "link", "rowformat")
MAX_KPIS = 12
SECTION_PREFIX = ">> "          # a table row `| >> INCOME STATEMENT | | |` becomes a merged section band
_DIRECTIVE_RX = re.compile(r"^\s*(" + "|".join(DIRECTIVES) + r")\s*:\s*(.*?)\s*$", re.I)
_SHEET_RX = re.compile(r"^\s*##\s*sheet\s*:\s*(.+?)\s*$", re.I)
_BLOCK_RX = re.compile(r"^\s*###\s*block\s*:\s*(.+?)\s*$", re.I)
MAX_BLOCKS = 16
_DASH_RX = re.compile(r"^\s*##\s*dashboard\s*:\s*(.+?)\s*$", re.I)
_TITLE_RX = re.compile(r"^\s*#\s+(.+?)\s*$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PCT = re.compile(r"^[-+]?\d+(?:\.\d+)?\s*%$")
_CURRENCY = re.compile(r"^(?P<neg>-)?\s*(?P<sym>৳|\$|£|€|₹|Tk\.?|BDT|USD|EUR|GBP|INR)\s*(?P<num>-?\d[\d,]*(?:\.\d+)?)$", re.I)
_PLAIN_NUM = re.compile(r"^[-+]?\d[\d,]*(?:\.\d+)?$")

CURRENCY_SYMBOLS = {"BDT": "৳", "USD": "$", "EUR": "€", "GBP": "£", "INR": "₹", "TK": "৳", "TK.": "৳"}

THEMES = {   # accent, light band
    "corporate": ("1F3864", "EEF2F9"), "forest": ("2E5E4E", "EDF4F0"), "sunset": ("D9480F", "FDF1EA"), "minimal": ("333333", "F3F3F3"),
    "royal": ("4B3F8C", "F1EFF9"), "ember": ("B45309", "FBF1E6"), "editorial": ("1F2937", "F2F3F5"), "ocean": ("0B5E80", "E9F4F9"),
}
DEFAULT_THEME = "corporate"

_FILLS = {"red": ("FFC7CE", "9C0006"), "green": ("C6EFCE", "006100"), "amber": ("FFEB9C", "9C5700"), "yellow": ("FFEB9C", "9C5700"),
          "blue": ("DDEBF7", "1F4E78"), "grey": ("E7E6E6", "404040"), "gray": ("E7E6E6", "404040")}


class BuildError(ValueError):
    pass


FONT_NAME = "Calibri"           # one typeface for header, body, banner, bands and notes of an auto-designed sheet
_FMT_RAW = r"[#0,.\-%$€£₹৳ \"A-Za-z/:@_()]+"
_FMT_RAW_AUTO = r"[#0,.\-%$€£₹৳ \"A-Za-z/:@_()\[\];]+"      # auto-design formats also use `;` sections and `[Red]`

# A decorative directive that cannot be applied (a highlight on a column that is not there, a chart with a wrong column name) must
# not throw the whole workbook away: it is skipped and reported, the model fixes it with doc_edit. Structural problems and refused
# formulas still stop the build.
_warnings: contextvars.ContextVar = contextvars.ContextVar("sheet_build_warnings", default=None)


def _warn(msg: str) -> None:
    w = _warnings.get()
    if w is not None and msg not in w:
        w.append(msg)


def warnings_active() -> bool:
    """True while a caller collects warnings (and will report them): only then may a problem be softened instead of raised."""
    return _warnings.get() is not None


def start_warnings() -> None:
    _warnings.set([])


def take_warnings() -> list:
    w = _warnings.get() or []
    _warnings.set(None)
    return list(w)


def warnings_note() -> str:
    """' Skipped: ...' for the tool result (and clears the list), '' when everything was applied."""
    w = take_warnings()
    return (" Skipped (not applied, fix with doc_edit): " + "; ".join(w[:6]) + (" ..." if len(w) > 6 else "") + ".") if w else ""


# What auto-design (autodesign.py) added to the workbook just built, for the tool result; a separate list from the warnings.
_autodesign_report: contextvars.ContextVar = contextvars.ContextVar("sheet_build_autodesign", default=None)


def take_autodesign_report() -> list:
    r = _autodesign_report.get() or []
    _autodesign_report.set(None)
    return list(r)


def autodesign_note() -> str:
    """One line for the tool result about the design added automatically (and clears the report), '' when nothing was added."""
    r = take_autodesign_report()
    if not r:
        return ""
    return (" Design added automatically: " + ", ".join(r[:6]) + (" ..." if len(r) > 6 else "") +
            ". Add directives only to change it; `style: plain` turns it off.")


def _mkfont(auto: bool):
    """openpyxl's Font, or (auto-designed sheet) a Font that always names the typeface and size."""
    from openpyxl.styles import Font
    if not auto:
        return Font

    def font(**kw):
        kw.setdefault("name", FONT_NAME)
        kw.setdefault("size", 11)
        return Font(**kw)
    return font


def _cols_hint(header: list) -> str:
    return f" (its columns: {', '.join(str(h) for h in header[:12])})"


# ---- parsing ---------------------------------------------------------------------------------------------------------
def is_outline(text: str) -> bool:
    """A workbook outline has `## Sheet:` headings, or a table with directive lines (format:, chart:, total: ...)."""
    t = str(text or "")
    if re.search(r"(?mi)^\s*##\s*(sheet|dashboard)\s*:", t):
        return True
    return bool(re.search(r"(?m)^\s*\|", t)) and bool(re.search(
        r"(?mi)^\s*(format|total|chart|highlight|scale|databar|validate|table|name|freeze|widths|merge|color|align|border|banner|subtitle|kpi|input|style|icons|rowformat|link)\s*:", t))


def _split_row(line: str) -> list:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.replace("\\|", "|").strip() for c in re.split(r"(?<!\\)\|", s)]


def _is_sep(cells: list) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c.strip()) or not c.strip() for c in cells)


def _csv_fields(line: str):
    """`a,b,"c, d"` -> ['a', 'b', 'c, d'], or None when the line is not comma-separated (fewer than two fields)."""
    import csv
    try:
        row = next(csv.reader([line], skipinitialspace=True))
    except (csv.Error, StopIteration):
        return None
    return row if len(row) >= 2 else None


def _csv_row(line: str, in_csv: bool, n_expected: int, next_line: str):
    """A table row written as CSV (a small model often does this under `## Sheet:`). Not a directive or heading. A table starts
    when the next line has at least as many fields as this one; inside one, a row with another number of fields is still a row
    (extra fields are joined into the last column, missing ones are left empty) unless it looks like the header of a new table."""
    if line.lstrip().startswith("#") or _DIRECTIVE_RX.match(line):
        return None
    fields = _csv_fields(line)
    if fields is None:
        return None
    n = len(fields)
    nxt = _csv_fields(next_line) if next_line and "|" not in next_line else None
    if in_csv:
        if n != n_expected and nxt and len(nxt) == n and not any(re.search(r"\d", f) for f in fields):
            return fields                                     # a header-like row followed by rows of its own width
        return fields
    return fields if nxt and len(nxt) >= n else None          # the row under a header may carry extra, unquoted commas


def _fit_row(fields: list, n: int) -> list:
    return fields[:n - 1] + [", ".join(fields[n - 1:])] if len(fields) > n else fields


_PIPE_RX = re.compile(r"(?<!\\)\|")


def _pipe_row(line: str, was_table: bool, next_line: str) -> bool:
    """A table row written without the outer pipes (`Metric | Value | Source`, valid GitHub markdown, and what a small model
    often writes). Not a directive or a heading; it needs two or more pipes, or to sit inside a table, or to be followed by (or
    be) a `--- | ---` separator."""
    if "|" not in line or line.lstrip().startswith("#") or _DIRECTIVE_RX.match(line):
        return False
    n = len(_PIPE_RX.findall(line))
    if n >= 2 or was_table:
        return True
    return _dash_sep(_split_row(line)) or (bool(next_line) and "|" in next_line and _dash_sep(_split_row(next_line)))


_EMPH_RX = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")


def _plain(s: str) -> str:
    """`**bold**` markers a model puts in table cells are not Excel: drop them (formulas are left alone; `__x__` is kept, it
    is usually an identifier)."""
    s = str(s)
    return s if s.lstrip().startswith("=") else _EMPH_RX.sub(r"\1", s)


def _dash_sep(cells: list) -> bool:
    """A real markdown separator row (`|---|---|`): at least one dashes cell, so an empty spacer row is not mistaken for one."""
    return _is_sep(cells) and any(re.fullmatch(r":?-{2,}:?", c.strip()) for c in cells)


def parse(text: str) -> dict:
    wb = {"title": "", "theme": DEFAULT_THEME, "sheets": [], "names": []}
    cur: Optional[dict] = None
    block: Optional[dict] = None        # the `### Block:` being read (a further table on the current sheet)
    pending_title = ""                  # the last text line that was not a table or directive: the title of the next table
    row_title = ""                      # the text that stood just above the table row read last

    def start(name, dashboard=False):
        nonlocal cur, block, pending_title
        cur = {"name": name, "header": None, "rows": [], "directives": [], "dashboard": dashboard, "blocks": []}
        wb["sheets"].append(cur)
        block = None
        pending_title = ""              # a text line from the end of the previous sheet is not this sheet's lead-in
    lines = [r.rstrip() for r in str(text or "").splitlines() if r.strip()]
    in_table = False                    # the previous kept line was a table row
    csv_n = 0                           # fields per row of the CSV table being read (0 = not in one)
    for li, line in enumerate(lines):
        was_table, in_table = in_table, False
        if not was_table:
            csv_n = 0
        m = _SHEET_RX.match(line)
        if m:
            start(m.group(1))
            continue
        m = _DASH_RX.match(line)
        if m:
            start(m.group(1), dashboard=True)
            continue
        m = _BLOCK_RX.match(line)
        if m and cur is not None and not cur["dashboard"]:
            parts = [x.strip() for x in m.group(1).split("|")]
            at = next((x.split("=", 1)[1].strip().upper() for x in parts[1:] if x.lower().startswith("at=")), "")
            block = {"title": parts[0], "at": at if re.fullmatch(r"[A-Z]{1,3}\d{1,7}", at) else "",
                     "header": None, "rows": [], "directives": []}
            cur["blocks"].append(block)
            continue
        if cur is None:
            t = _TITLE_RX.match(line)
            if t and not wb["title"]:
                wb["title"] = t.group(1)
                continue
            d = re.match(r"^\s*theme\s*:\s*(\S+)", line, re.I)
            if d:
                wb["theme"] = d.group(1).lower()
                continue
            d = re.match(r"^\s*style\s*:\s*(\S+)", line, re.I)
            if d:
                wb["style"] = d.group(1).lower()
                continue
        if cur is not None and not cur["dashboard"] and "|" not in line:
            fields = _csv_row(line, was_table and csv_n > 0, csv_n, lines[li + 1] if li + 1 < len(lines) else "")
            if fields is not None:
                in_table = True
                n = len(fields)
                target = block if block is not None else cur
                new_table = bool(was_table and csv_n and n != csv_n and not any(re.search(r"\d", f) for f in fields)
                                 and target["header"] is not None)
                if new_table:                                  # a header-like row of another width: a further table
                    block = {"title": pending_title or f"Table {len(cur['blocks']) + 2}", "at": "",
                             "header": [_plain(f.strip()) for f in fields], "rows": [], "directives": []}
                    cur["blocks"].append(block)
                    pending_title, csv_n = "", n
                    continue
                if not (was_table and csv_n):
                    csv_n = n
                cells = [_plain(f.strip()) for f in _fit_row(fields, csv_n)]
                row_title, pending_title = pending_title, ""
                if target["header"] is None:
                    target["header"] = cells
                    if target is cur:
                        cur["lead"] = row_title
                else:
                    target["rows"].append(cells)
                continue
        if line.lstrip().startswith("|") or (
                cur is not None and not cur["dashboard"]
                and _pipe_row(line, was_table, lines[li + 1] if li + 1 < len(lines) else "")):
            in_table = True
            if cur is None:
                start("Sheet1")
            cells = [_plain(c) for c in _split_row(line)]
            target = block if block is not None else cur
            if _dash_sep(cells):
                if target["header"] is not None and target["rows"]:
                    # a separator right under a data row: that row was the HEADER of a further table on this sheet (a model
                    # writes several tables under one `## Sheet:`). Keep it as its own block instead of mixing its rows,
                    # with the other table's columns, into this one.
                    hdr = target["rows"].pop()
                    block = {"title": row_title or f"Table {len(cur['blocks']) + 2}", "at": "", "header": hdr,
                             "rows": [], "directives": []}
                    cur["blocks"].append(block)
                continue
            if _is_sep(cells):
                continue
            row_title, pending_title = pending_title, ""
            if target["header"] is None:
                target["header"] = cells
                if target is cur:
                    cur["lead"] = row_title       # the text line above the main table: auto-design's subtitle
            else:
                target["rows"].append(cells)
            continue
        d = _DIRECTIVE_RX.match(line)
        if d:
            key, val = d.group(1).lower(), d.group(2)
            if key == "name":
                wb["names"].append(val)
            else:
                if cur is None:
                    start("Sheet1")
                (block if block is not None and key in ("format", "total") else cur)["directives"].append((key, val))
        elif cur is not None:
            pending_title = _plain(line).strip().lstrip("#").strip().rstrip(":").strip()[:80]
            cur.setdefault("sample", line.strip()[:90])
    keep = [s for s in wb["sheets"]
            if s["header"] or (s["dashboard"] and any(k in ("kpi", "chart") for k, _ in s["directives"]))]
    wb["dropped"] = [(s["name"], s.get("sample", "")) for s in wb["sheets"] if s not in keep]     # sheets with no table at all
    wb["sheets"] = keep
    return wb


# ---- cell values and formats -----------------------------------------------------------------------------------------
def _currency_format(code: str) -> str:
    sym = CURRENCY_SYMBOLS.get(code.upper().strip(), code if len(code) <= 3 else "")
    return f'"{sym}"#,##0.00' if sym else "#,##0.00"


def _named_format(spec: str) -> Optional[str]:
    s = spec.strip()
    low = s.lower()
    if low.startswith("currency"):
        return _currency_format(s.split(":", 1)[1] if ":" in s else "BDT")
    return {"percent": "0.0%", "pct": "0.0%", "date": "yyyy-mm-dd", "datetime": "yyyy-mm-dd hh:mm", "int": "#,##0",
            "integer": "#,##0", "number": "#,##0.00", "decimal": "#,##0.00", "text": "@", "general": "General"}.get(low)


def _infer(values: list) -> Optional[str]:
    vals = [v for v in values if v not in (None, "") and not str(v).startswith("=")]
    if not vals:
        return None
    if all(_ISO_DATE.match(str(v)) for v in vals):
        return "date"
    if all(_PCT.match(str(v)) for v in vals):
        return "percent"
    cur = [_CURRENCY.match(str(v).strip()) for v in vals]
    if all(cur):
        return "currency:" + (cur[0].group("sym").upper().rstrip("."))
    return None


def _to_number(s: str):
    t = str(s).strip()
    if _PCT.match(t):
        return round(float(t.rstrip("% ").replace(" ", "")) / 100, 10)        # 57.2% -> 0.572, not 0.5720000000000001
    m = _CURRENCY.match(t)
    if m:
        n = float(m.group("num").replace(",", ""))
        return -n if m.group("neg") else n
    if _PLAIN_NUM.match(t):
        digits = t.lstrip("+-").split(".")[0].replace(",", "")
        if len(digits) > 1 and digits.startswith("0") and "." not in t:       # 007 / 01234 are identifiers
            return None
        if len(digits) > 15:
            return None
        return float(t.replace(",", "")) if "." in t else int(t.replace(",", ""))
    return None


def _quoted(text: str) -> str:
    """A text value as an Excel string literal for a conditional-format rule: inner quotes doubled, so the text can never
    close the literal and continue as a formula."""
    return '"' + str(text).replace('"', '""') + '"'


def _cell_value(raw, fmt: Optional[str], sheets: list, where: str, formulas: bool = True):
    """(value, is_formula). A formula that is not allowed (unknown or unsafe function, external link ...) is stored as text and
    reported (_warn): it never runs, and the rest of the workbook is still built. With formulas=False (a plain table the author did
    not mark up) a cell that starts with = stays text, as it always has."""
    s = "" if raw is None else str(raw).strip()
    if s == "":
        return None, False
    if s.startswith("=") and not formulas:
        return s, False
    if s.startswith("="):
        try:
            # a sheet that does not exist YET is allowed (a workbook is built sheet by sheet: the Summary sheet is written first and
            # points at sheets added later). Everything else is still checked; a wrong sheet name shows up as #REF! in the check report.
            return to_file_form(check_formula(s, sheets=None)), True
        except FormulaError as e:
            _warn(f"{where}: {e}. The cell was kept as plain text (it is not a formula); write it with a supported function")
            return s, False
    if fmt == "date" and _ISO_DATE.match(s):
        import datetime as dt
        try:
            return dt.date.fromisoformat(s), False
        except ValueError:
            return s, False
    if fmt in ("percent", "int", "number") or (fmt or "").startswith("currency") or fmt is None:
        n = _to_number(s)
        if n is not None:
            return n, False
    return s, False


def _safe_sheet_name(name: str, used: set) -> str:
    base = re.sub(r"[\[\]:*?/\\]", "-", str(name)).strip().strip("'")[:31] or "Sheet"
    out, n = base, 2
    while out.lower() in used:
        suffix = f" {n}"
        out, n = base[: 31 - len(suffix)] + suffix, n + 1
    used.add(out.lower())
    return out


def col_letter(n: int) -> str:
    from openpyxl.utils import get_column_letter
    return get_column_letter(n)


def _kv_pairs(text: str) -> dict:
    out = {}
    for part in re.split(r"(?:,\s*|\s+)(?=[A-Za-z0-9_]+=)", text.strip()):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().lower()] = v.strip().strip("\"'")
    return out


# ---- building --------------------------------------------------------------------------------------------------------
def build(text: str, title: str = "", formulas: bool = True) -> bytes:
    import openpyxl
    from openpyxl.styles import Alignment, Border, PatternFill, Side
    from .autodesign import acct_number, enrich
    _warnings.set([])
    spec = parse(text)
    spec, report = enrich(spec)
    _autodesign_report.set(report)
    if not any(s["header"] for s in spec["sheets"]):
        seen = "".join(f" Sheet '{n}' starts with: \"{smp}\"." for n, smp in spec.get("dropped", [])[:2] if smp)
        raise BuildError("no sheet with a table found - write `## Sheet: Name` followed by a markdown table: a header line "
                         "`| Metric | Value |`, the line `|---|---|`, then one `| a | b |` line per row." + seen)
    for n, smp in spec.get("dropped", []):
        _warn(f"sheet '{n}' has no table (it starts with \"{smp[:60]}\") and was left out; write it as a markdown table")
    if len(spec["sheets"]) > MAX_SHEETS:
        raise BuildError(f"{len(spec['sheets'])} sheets written, at most {MAX_SHEETS} per workbook: merge related sheets "
                         "(several tables can sit under one `## Sheet:`, each with its own header) and send it again")
    if sum(len(s["rows"]) for s in spec["sheets"]) > MAX_ROWS_TOTAL:
        raise BuildError(f"too many rows (limit {MAX_ROWS_TOTAL:,}); build the rest with doc_edit append_rows")
    accent, band = THEMES.get(spec["theme"], THEMES[DEFAULT_THEME])
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    used: set = set()
    names = []
    for s in spec["sheets"]:
        s["xname"] = _safe_sheet_name(s["name"], used)
        names.append(s["xname"])
    by_name = {s["name"].lower(): s for s in spec["sheets"]}
    by_name.update({s["xname"].lower(): s for s in spec["sheets"]})
    thin = Side(style="thin", color="D0D5DD")
    charts = []

    for s in spec["sheets"]:
        ws = wb.create_sheet(s["xname"])
        auto_s = bool(spec.get("auto") and s.get("auto", True))      # auto-design on for this sheet
        if not s["header"]:                      # a dashboard: KPI tiles and charts, no table
            charts += _dashboard(ws, s, spec, accent, band, names, formulas)
            if auto_s:
                ws.oddFooter.center.text = "&A - Page &P of &N"
            continue
        Font = _mkfont(auto_s)
        if s.get("nogrid"):                      # the Contents sheet that auto-design adds
            ws.sheet_view.showGridLines = False
        numcols = s.get("numcols", set())        # columns auto-design found to be numbers (lower-case header names)
        header = [h or f"Column{i + 1}" for i, h in enumerate(s["header"][:MAX_COLS])]
        ncols = len(header)
        rows = [(r + [""] * ncols)[:ncols] for r in s["rows"]]
        dirs = {}
        for k, v in s["directives"]:
            dirs.setdefault(k, []).append(v)
        banner = (dirs.get("banner") or [""])[-1].strip()
        subtitle = (dirs.get("subtitle") or [""])[-1].strip()
        top = (1 if banner else 0) + (1 if subtitle else 0)
        H = top + 2 if top else 1                 # header row: banner/subtitle rows, one blank row, then the table
        s["hdr"] = H                              # charts on other sheets read the table position from here
        # section bands: `| >> TITLE | | |` (first cell only) -> index of the row in `rows`
        sec = {i for i, r in enumerate(rows) if str(r[0]).startswith(SECTION_PREFIX) and not any(str(x).strip() for x in r[1:])}
        body_rows = [r for i, r in enumerate(rows) if i not in sec]
        fmt_map = {}
        for v in dirs.get("format", []):
            for part in re.split(r"\s*,\s*(?=[^,=]+=)", v):
                if "=" in part:
                    k, f = part.split("=", 1)
                    fmt_map[k.strip().lower()] = _named_format(f) or (f.strip() if re.fullmatch(_FMT_RAW_AUTO if auto_s else _FMT_RAW, f.strip()) else None)
                    fmt_map.setdefault("__kind__" + k.strip().lower(), f.strip().lower())
        row_fmt = {}                              # rowformat: Label=format  (first-column label of the row -> number format)
        for v in dirs.get("rowformat", []):
            lab, _, f = v.rpartition("=")
            f = f.strip()
            nf_ = _named_format(f) or (f if re.fullmatch(_FMT_RAW_AUTO if auto_s else _FMT_RAW, f) else None)
            if lab.strip() and nf_:
                row_fmt[lab.strip().lower()] = nf_
            else:
                _warn(f"sheet '{s['name']}': rowformat '{v}': expects `Row label=format` (0.0%, #,##0, ...)")
        col_fmt = []
        for ci, h in enumerate(header):
            col_vals = [r[ci] for r in body_rows]
            explicit = fmt_map.get(h.lower())
            kind = fmt_map.get("__kind__" + h.lower()) or _infer(col_vals)
            col_fmt.append((kind, explicit or (_named_format(kind) if kind else None)))

        # banner / subtitle rows above the table
        if top:
            r_top = 1
            for text_, is_banner in ((banner, True), (subtitle, False)):
                if not text_:
                    continue
                c = ws.cell(row=r_top, column=1, value=text_)
                c.data_type = "s"
                if is_banner:
                    c.font = Font(bold=True, size=14, color="FFFFFF")
                    for ci in range(1, ncols + 1):
                        ws.cell(row=r_top, column=ci).fill = PatternFill("solid", fgColor=accent)
                    c.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True, indent=1)
                    ws.row_dimensions[r_top].height = 28
                else:
                    c.font = Font(italic=True, color="666666")
                    c.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
                    ws.row_dimensions[r_top].height = max(18, 15 * -(-len(text_) // max(60, ncols * 12)))
                if ncols > 1:
                    ws.merge_cells(start_row=r_top, start_column=1, end_row=r_top, end_column=ncols)
                r_top += 1
        # header
        for ci, h in enumerate(header, 1):
            c = ws.cell(row=H, column=ci, value=h)
            c.data_type = "s"                      # a header is text, even one that starts with "="
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor=accent)
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        # body
        for ri, row in enumerate(rows, H + 1):
            if ri - H - 1 in sec:                  # a section band: text only, styled after banding below
                ws.cell(row=ri, column=1, value=str(row[0])[len(SECTION_PREFIX):].strip()).data_type = "s"
                continue
            for ci, raw in enumerate(row, 1):
                kind, nf = col_fmt[ci - 1]
                if ci > 1 and row_fmt:
                    nf = row_fmt.get(str(row[0]).strip().lower(), nf)
                where = f"sheet '{s['name']}' cell {col_letter(ci)}{ri}"
                isnum = header[ci - 1].lower() in numcols
                # a number column with a custom format string: its cells still have to become numbers
                vkind = "number" if isnum and kind not in (None, "percent", "int", "number", "text", "@", "general", "date", "datetime")                     and not kind.startswith("currency") else kind
                val, is_formula = _cell_value(raw, vkind, names, where, formulas)
                if isnum and isinstance(val, str) and not is_formula:
                    val = acct_number(val, val)               # (1,234) is minus 1,234
                c = ws.cell(row=ri, column=ci)
                if auto_s:
                    c.font = Font()
                if is_formula:
                    c.value = val
                elif val is not None:
                    c.value = val
                    if isinstance(val, str):
                        c.data_type = "s"          # plain text never becomes a formula
                if nf and (is_formula or isinstance(val, (int, float)) or hasattr(val, "isoformat")):
                    c.number_format = nf
                elif isinstance(val, float) and isinstance(raw, str) and _PCT.match(raw.strip()):
                    c.number_format = "0.0%"       # a "46.2%" cell in a mixed column still shows as a percent, not 0.462
                c.border = Border(bottom=thin)
                if isinstance(val, (int, float)) or is_formula:
                    c.alignment = Alignment(horizontal="right", vertical="top")
                elif isinstance(val, str) and len(val) > 45:
                    c.alignment = Alignment(wrap_text=True, vertical="top")      # long text wraps instead of running off the sheet
                else:
                    c.alignment = Alignment(vertical="top")
        last = len(rows) + H
        from ..doc_ops.xlsx_ops.financial_styles import is_total_row as _is_tot
        if rows and _is_tot(str(rows[-1][0] if rows[-1] else "")):
            for ci in range(1, ncols + 1):
                cell = ws.cell(row=last, column=ci)
                cell.font = Font(bold=True)
                cell.border = Border(top=Side(style="thin", color="94A3B8"), bottom=Side(style="double", color="0F172A"))

        # totals
        total_row = None
        for v in dirs.get("total", []):
            total_row = last + 1
            codes = {"sum": 109, "avg": 101, "average": 101, "min": 105, "max": 104, "count": 103}
            ws.cell(row=total_row, column=1, value="Total").font = Font(bold=True)
            for part in re.split(r"\s*,\s*", v):
                if "=" not in part:
                    continue
                cname, fn = (x.strip() for x in part.split("=", 1))
                idx = next((i for i, h in enumerate(header, 1) if h.lower() == cname.lower()), None)
                if idx is None or fn.lower() not in codes:
                    _warn(f"sheet '{s['name']}': total {cname}={fn}: needs Column=sum|avg|min|max|count with a column that exists" + _cols_hint(header))
                    continue
                cell = ws.cell(row=total_row, column=idx, value=f"=SUBTOTAL({codes[fn.lower()]},{col_letter(idx)}{H + 1}:{col_letter(idx)}{last})")
                cell.font = Font(bold=True)
                if col_fmt[idx - 1][1]:
                    cell.number_format = col_fmt[idx - 1][1]
                cell.alignment = Alignment(horizontal="right")
            for ci in range(1, ncols + 1):
                ws.cell(row=total_row, column=ci).border = Border(top=Side(style="medium", color=accent))

        # banding (a real Excel table brings its own)
        table_name = (dirs.get("table") or [""])[0].strip()
        if not table_name:
            for ri in range(H + 2, last + 1, 2):
                for ci in range(1, ncols + 1):
                    ws.cell(row=ri, column=ci).fill = PatternFill("solid", fgColor=band)
        for i in sorted(sec):                      # section bands: merged, bold, accent text on the band colour
            ri = H + 1 + i
            for ci in range(1, ncols + 1):
                ws.cell(row=ri, column=ci).fill = PatternFill("solid", fgColor=band)
                ws.cell(row=ri, column=ci).border = Border(bottom=thin)
            c = ws.cell(row=ri, column=1)
            c.font = Font(bold=True, color=accent)
            c.alignment = Alignment(horizontal="left", vertical="center")
            if ncols > 1:
                ws.merge_cells(start_row=ri, start_column=1, end_row=ri, end_column=ncols)

        # widths
        for ci, h in enumerate(header, 1):
            longest = max([_text_width(h)] + [min(_text_width(r[ci - 1]), 40) if not str(r[ci - 1]).startswith("=") else 12 for r in body_rows] or [8])
            longest += _format_room(col_fmt[ci - 1][1], longest)
            ws.column_dimensions[col_letter(ci)].width = min(max(longest + 3, 9), 60)
        for v in dirs.get("widths", []):           # widths: B=22, Notes=60   (a column letter or a header name)
            pairs = []
            for chunk in re.split(r"\s*,\s*", v.strip()):
                # `A=10 B=20` (space separated, the older form) still works; a name with spaces needs a comma
                pairs += re.split(r"\s+(?=[A-Za-z0-9_]+=)", chunk) if chunk.count("=") > 1 else [chunk]
            for part in pairs:
                if "=" not in part:
                    continue
                k, w = (x.strip().strip("\"'") for x in part.rsplit("=", 1))
                ci = next((i for i, h in enumerate(header, 1) if h.lower() == k.lower()), None)
                letter = col_letter(ci) if ci else (k.upper() if re.fullmatch(r"[A-Za-z]{1,3}", k) else None)
                if letter is None:
                    _warn(f"sheet '{s['name']}': widths: column '{k}' not found" + _cols_hint(header))
                    continue
                try:
                    ws.column_dimensions[letter].width = float(w)
                except ValueError:
                    pass

        # freeze, filter, table, print
        freeze = (dirs.get("freeze") or [f"A{H + 1}"])[-1].strip()
        if freeze.lower() not in ("none", "off", "no"):
            ws.freeze_panes = freeze.upper() if re.fullmatch(r"[A-Za-z]{1,3}\d+", freeze) else f"A{H + 1}"
        flt = (dirs.get("filter") or ["on"])[-1].strip().lower()
        if table_name:
            from openpyxl.worksheet.table import Table, TableStyleInfo
            safe = re.sub(r"[^A-Za-z0-9_]", "_", table_name)
            if not re.match(r"[A-Za-z_]", safe):
                safe = "t_" + safe
            tb = Table(displayName=safe, ref=f"A{H}:{col_letter(ncols)}{last}")
            tb.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
            ws.add_table(tb)
        elif flt not in ("off", "no", "none") and rows:
            ws.auto_filter.ref = f"A{H}:{col_letter(ncols)}{last}"
        ws.page_setup.orientation = "landscape" if ncols > 6 else "portrait"
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
        ws.sheet_properties.pageSetUpPr = openpyxl.worksheet.properties.PageSetupProperties(fitToPage=True)
        ws.print_title_rows = f"{H}:{H}"
        for v in dirs.get("print", []):            # print: landscape | portrait | fit=width | fit=page | no-repeat-header
            for word in re.split(r"[\s,|]+", v.strip().lower()):
                if word in ("landscape", "portrait"):
                    ws.page_setup.orientation = word
                elif word == "fit=page":
                    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 1
                elif word == "fit=width":
                    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
                elif word == "no-repeat-header":
                    ws.print_title_rows = None
                elif word:
                    _warn(f"sheet '{s['name']}': print: '{word}' is not known (landscape, portrait, fit=width, fit=page, no-repeat-header)")

        def _col_of(name: str):
            name = name.strip().lower()
            return next((i for i, h in enumerate(header, 1) if h.lower() == name), None)
        for v in dirs.get("wrap", []):             # wrap: Notes, Description
            for name in [x for x in v.split(",") if x.strip()]:
                ci = _col_of(name)
                if ci is None:
                    _warn(f"sheet '{s['name']}': wrap: column '{name.strip()}' not found" + _cols_hint(header))
                    continue
                for ri in range(H + 1, last + 1):
                    cell = ws.cell(row=ri, column=ci)
                    cell.alignment = Alignment(wrap_text=True, vertical="top", horizontal=cell.alignment.horizontal)
                ws.column_dimensions[col_letter(ci)].width = min(max(ws.column_dimensions[col_letter(ci)].width or 30, 30), 60)
        for v in dirs.get("hide", []):             # hide: Internal id, Cost   |   hide: sheet
            for name in [x for x in v.split(",") if x.strip()]:
                if name.strip().lower() == "sheet":
                    if sum(1 for x in spec["sheets"] if not x.get("hidden")) > 1:
                        s["hidden"] = True
                        ws.sheet_state = "hidden"
                    else:
                        _warn(f"sheet '{s['name']}': hide: a workbook needs at least one visible sheet")
                    continue
                ci = _col_of(name)
                if ci is None:
                    _warn(f"sheet '{s['name']}': hide: column '{name.strip()}' not found" + _cols_hint(header))
                else:
                    ws.column_dimensions[col_letter(ci)].hidden = True
        for v in dirs.get("tab", []):
            if re.fullmatch(r"[0-9A-Fa-f]{6}", v.strip()):
                ws.sheet_properties.tabColor = v.strip().upper()
        if auto_s:
            if ws.sheet_properties.tabColor is None:
                ws.sheet_properties.tabColor = accent
            ws.oddFooter.center.text = "&A - Page &P of &N"
            if spec["title"]:
                ws.oddHeader.left.text = spec["title"].replace("&", "&&")        # & starts a code in header text

        # conditional formatting
        from openpyxl.formatting.rule import CellIsRule, ColorScaleRule, DataBarRule, IconSetRule
        rng = lambda ci: f"{col_letter(ci)}{H + 1}:{col_letter(ci)}{max(last, H + 1)}"      # noqa: E731
        for v in dirs.get("highlight", []):
            m = re.match(r"^(?P<col>.+?)\s*(?P<op>>=|<=|!=|<>|=|>|<|\bis\b)\s*(?P<val>.+?)\s+(?P<color>red|green|amber|yellow|blue|grey|gray)$", v.strip(), re.I)
            m_bw = re.match(r"^(?P<col>.+?)\s+between\s+(?P<a>.+?)\s+and\s+(?P<b>.+?)\s+(?P<color>red|green|amber|yellow|blue|grey|gray)$", v.strip(), re.I)
            if not (m or m_bw):
                _warn(f"sheet '{s['name']}': highlight '{v}': expects `Column<value red` or `Column between a and b color`")
                continue
            cname = (m or m_bw).group("col").strip()
            idx = next((i for i, h in enumerate(header, 1) if h.lower() == cname.lower()), None)
            if idx is None:
                _warn(f"sheet '{s['name']}': highlight: column '{cname}' not found" + _cols_hint(header))
                continue
            color_name = (m or m_bw).group("color").lower()
            bg, fg = _FILLS[color_name]
            if m_bw:
                a_val, b_val = m_bw.group("a").strip(), m_bw.group("b").strip()
                fa = a_val if _to_number(a_val) is not None else _quoted(a_val)
                fb = b_val if _to_number(b_val) is not None else _quoted(b_val)
                ws.conditional_formatting.add(rng(idx), CellIsRule(operator="between", formula=[fa, fb],
                                              fill=PatternFill("solid", bgColor=bg, fgColor=bg), font=Font(color=fg)))
            else:
                op_raw = m.group("op").lower()
                op = {"=": "equal", "is": "equal", ">": "greaterThan", "<": "lessThan",
                      ">=": "greaterThanOrEqual", "<=": "lessThanOrEqual", "!=": "notEqual", "<>": "notEqual"}[op_raw]
                val = m.group("val").strip().strip("'\"")
                formula = val if _to_number(val) is not None else _quoted(val)
                ws.conditional_formatting.add(rng(idx), CellIsRule(operator=op, formula=[formula],
                                              fill=PatternFill("solid", bgColor=bg, fgColor=bg), font=Font(color=fg)))
        for key in ("scale", "databar", "icons"):
            for v in dirs.get(key, []):
                reverse = v.strip().lower().endswith(" reverse")
                cname = re.sub(r"\s+reverse$", "", v.strip(), flags=re.I)
                idx = next((i for i, h in enumerate(header, 1) if h.lower() == cname.lower()), None)
                if idx is None:
                    _warn(f"sheet '{s['name']}': {key}: column '{cname}' not found" + _cols_hint(header))
                    continue
                if key == "icons":
                    # three arrows: down below 0, flat at 0, up above 0 (the third threshold is strict)
                    rule = IconSetRule("3Arrows", "num", [0, 0, 0])
                    rule.iconSet.cfvo[0].type = "percent"
                    rule.iconSet.cfvo[2].gte = False
                    ws.conditional_formatting.add(rng(idx), rule)
                elif key == "scale":
                    lo, mid, hi = ("63BE7B", "FFEB84", "F8696B") if reverse else ("F8696B", "FFEB84", "63BE7B")
                    ws.conditional_formatting.add(rng(idx), ColorScaleRule(start_type="min", start_color=lo, mid_type="percentile",
                                                                          mid_value=50, mid_color=mid, end_type="max", end_color=hi))
                else:
                    ws.conditional_formatting.add(rng(idx), DataBarRule(start_type="min", end_type="max", color=accent))

        # data validation
        from openpyxl.worksheet.datavalidation import DataValidation
        for v in dirs.get("validate", []):
            m = re.match(r"^(?P<col>.+?)\s+in\s*\[(?P<items>.*)\]\s*(?P<soft>soft)?\s*$", v.strip(), re.I)
            m2 = re.match(r"^(?P<col>.+?)\s+between\s+(?P<a>-?[\d.]+)\s+and\s+(?P<b>-?[\d.]+)\s*$", v.strip(), re.I)
            if not (m or m2):
                _warn(f"sheet '{s['name']}': validate '{v}': expects `Column in [a, b]` or `Column between 0 and 100`")
                continue
            cname = (m or m2).group("col").strip()
            idx = next((i for i, h in enumerate(header, 1) if h.lower() == cname.lower()), None)
            if idx is None:
                _warn(f"sheet '{s['name']}': validate: column '{cname}' not found" + _cols_hint(header))
                continue
            if m:
                items = [x.strip() for x in m.group("items").split(",") if x.strip()]
                dv = DataValidation(type="list", formula1='"' + ",".join(items).replace('"', "") + '"', allow_blank=True)
            else:
                a, b = m2.group("a"), m2.group("b")
                dv = DataValidation(type="whole" if re.fullmatch(r"-?\d+", a + b.replace("-", "")) and "." not in a + b else "decimal",
                                    operator="between", formula1=a, formula2=b, allow_blank=True)
            dv.error, dv.errorTitle, dv.showErrorMessage = "Please pick a valid value", "Invalid entry", True
            if m and m.group("soft"):              # soft: the list is offered, anything else only warns (auto-design's lists)
                dv.errorStyle, dv.error, dv.errorTitle = "warning", "Not in the list. Keep it anyway?", "Not a listed value"
            ws.add_data_validation(dv)
            dv.add(f"{col_letter(idx)}{H + 1}:{col_letter(idx)}{max(last + 200, 500)}")      # a few hundred rows of room to add data

        # cell merging (rows and columns)
        for v in dirs.get("merge", []):
            parts = [p.strip() for p in re.split(r",\s*(?=[A-Za-z0-9_\"':])", v) if p.strip()]
            for part in parts:
                val_to_set = None
                if "=" in part and not re.match(r"^[A-Za-z]{1,3}\d+:", part):
                    val_to_set, rng_part = part.split("=", 1)
                    val_to_set = val_to_set.strip().strip('"\'')
                    rng_part = rng_part.strip()
                else:
                    rng_part = part
                m = re.match(r"^([A-Za-z]{1,3}\d+)\s*:\s*([A-Za-z]{1,3}\d+)$", rng_part)
                if not m:
                    _warn(f"sheet '{s['name']}': merge '{part}': expects format `A1:D1` or `\"Title\"=A1:D1`")
                    continue
                start_ref, end_ref = m.group(1).upper(), m.group(2).upper()
                try:
                    ws.merge_cells(f"{start_ref}:{end_ref}")
                    top_cell = ws[start_ref]
                    if val_to_set is not None:
                        top_cell.value = val_to_set
                        if isinstance(val_to_set, str):
                            top_cell.data_type = "s"       # a title is text, never a formula
                        top_cell.font = Font(bold=True)
                        top_cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
                except Exception as e:
                    _warn(f"sheet '{s['name']}': merge '{part}': {e}")

        # custom cell and range coloring
        for v in dirs.get("color", []):
            tokens = v.strip().split(None, 1)
            if not tokens:
                continue
            target = tokens[0].strip()
            rest = tokens[1] if len(tokens) > 1 else ""
            opts = _kv_pairs(rest)
            if not opts and "=" in v:
                opts = _kv_pairs(v)
                target = list(opts.keys())[0] if opts else ""
            fill_c = opts.get("fill") or opts.get("bg")
            font_c = opts.get("font") or opts.get("fg") or opts.get("color")
            is_bold = opts.get("bold", "").lower() in ("true", "1", "yes")
            align_h = opts.get("align", "").lower() or None

            target_cells = []
            m_rng = re.match(r"^([A-Za-z]{1,3}\d+)(?::([A-Za-z]{1,3}\d+))?$", target, re.I)
            if m_rng:
                start_c = m_rng.group(1).upper()
                end_c = m_rng.group(2).upper() if m_rng.group(2) else start_c
                for r_row in ws[f"{start_c}:{end_c}"]:
                    for c_cell in (r_row if isinstance(r_row, tuple) else [r_row]):
                        target_cells.append(c_cell)
            elif target.lower() == "total" and total_row:
                for ci in range(1, ncols + 1):
                    target_cells.append(ws.cell(row=total_row, column=ci))
            else:
                idx = next((i for i, h in enumerate(header, 1) if h.lower() == target.lower()), None)
                if idx is not None:
                    for ri in range(H + 1, last + 1):
                        target_cells.append(ws.cell(row=ri, column=idx))

            if not target_cells:
                _warn(f"sheet '{s['name']}': color: target '{target}' not found (use cell range like A1:D1, column name, or 'total')")
                continue

            fg_hex = None
            if fill_c:
                clean_fill = fill_c.lstrip("#").lower()
                if clean_fill in _FILLS:
                    fg_hex = _FILLS[clean_fill][0]
                elif clean_fill in THEMES:
                    fg_hex = THEMES[clean_fill][0]
                elif re.fullmatch(r"[0-9A-Fa-f]{6}", clean_fill):
                    fg_hex = clean_fill.upper()

            txt_hex = None
            if font_c:
                clean_font = font_c.lstrip("#").lower()
                if clean_font in ("white", "w"):
                    txt_hex = "FFFFFF"
                elif clean_font in ("black", "b"):
                    txt_hex = "000000"
                elif clean_font in _FILLS:
                    txt_hex = _FILLS[clean_font][1]
                elif re.fullmatch(r"[0-9A-Fa-f]{6}", clean_font):
                    txt_hex = clean_font.upper()

            for cell in target_cells:
                if fg_hex:
                    cell.fill = PatternFill("solid", fgColor=fg_hex)
                if txt_hex or is_bold:
                    cf = cell.font or Font()
                    cell.font = Font(
                        name=cf.name or "Calibri",
                        size=cf.size or 11,
                        bold=is_bold if is_bold else cf.bold,
                        italic=cf.italic,
                        color=txt_hex if txt_hex else cf.color
                    )
                if align_h and align_h in ("left", "center", "right"):
                    cv = cell.alignment.vertical if cell.alignment else "center"
                    cell.alignment = Alignment(horizontal=align_h, vertical=cv)

        # input cells: yellow fill + blue text, the convention for "you may change this" (input: Rate, Base)
        for v in dirs.get("input", []):
            for name in [x for x in v.split(",") if x.strip()]:
                ci = _col_of(name)
                if ci is None:
                    _warn(f"sheet '{s['name']}': input: column '{name.strip()}' not found" + _cols_hint(header))
                    continue
                for ri in range(H + 1, last + 1):
                    if ri - H - 1 in sec:
                        continue
                    cell = ws.cell(row=ri, column=ci)
                    cell.fill = PatternFill("solid", fgColor="FFF2CC")
                    cell.font = Font(color="0000FF", bold=bool(cell.font and cell.font.bold))

        # explicit alignment
        for v in dirs.get("align", []):
            for part in re.split(r"\s*,\s*", v.strip()):
                if "=" not in part:
                    continue
                col_ref, side = (x.strip() for x in part.split("=", 1))
                side = side.lower()
                if side not in ("left", "center", "right"):
                    continue
                m_single = re.match(r"^([A-Za-z]{1,3})(\d+)?$", col_ref)
                if m_single and m_single.group(2):
                    ws[col_ref.upper()].alignment = Alignment(horizontal=side)
                else:
                    idx = None
                    if m_single and not m_single.group(2) and len(col_ref) <= 3:
                        try:
                            from openpyxl.utils import column_index_from_string
                            idx = column_index_from_string(col_ref)
                        except Exception:
                            pass
                    if idx is None:
                        idx = next((i for i, h in enumerate(header, 1) if h.lower() == col_ref.lower()), None)
                    if idx is not None:
                        for ri in range(H + 1, last + 1):
                            ws.cell(row=ri, column=idx).alignment = Alignment(horizontal=side)

        # explicit borders
        for v in dirs.get("border", []):
            tokens = v.strip().split()
            if not tokens:
                continue
            b_target = tokens[0].strip()
            b_style = tokens[1].lower() if len(tokens) > 1 else "thin"
            if b_style not in ("thin", "medium", "thick", "double", "dashed", "dotted"):
                b_style = "thin"
            b_side = Side(style=b_style, color=accent if b_style in ("medium", "thick") else "D9D9D9")
            m_rng = re.match(r"^([A-Za-z]{1,3}\d+)(?::([A-Za-z]{1,3}\d+))?$", b_target, re.I)
            if m_rng:
                start_c = m_rng.group(1).upper()
                end_c = m_rng.group(2).upper() if m_rng.group(2) else start_c
                for r_row in ws[f"{start_c}:{end_c}"]:
                    for c_cell in (r_row if isinstance(r_row, tuple) else [r_row]):
                        c_cell.border = Border(top=b_side, bottom=b_side, left=b_side, right=b_side)
            elif b_target.lower() == "total" and total_row:
                for ci in range(1, ncols + 1):
                    ws.cell(row=total_row, column=ci).border = Border(top=Side(style="medium", color=accent),
                                                                      bottom=Side(style="double", color=accent))

        # link: Sheet   (the column's cells name sheets of this workbook; each becomes an internal link to that sheet's A1)
        from openpyxl.worksheet.hyperlink import Hyperlink
        for v in dirs.get("link", []):
            ci = _col_of(v)
            if ci is None:
                _warn(f"sheet '{s['name']}': link: column '{v.strip()}' not found" + _cols_hint(header))
                continue
            for ri in range(H + 1, last + 1):
                if ri - H - 1 in sec:
                    continue
                target = by_name.get(str(rows[ri - H - 1][ci - 1]).strip().lower())
                if target is None:
                    continue
                cell = ws.cell(row=ri, column=ci)
                cell.hyperlink = Hyperlink(ref=cell.coordinate, location="'" + target["xname"].replace("'", "''") + "'!A1")
                cell.font = Font(color="0563C1", underline="single")

        # notes under the table
        nrow = (total_row or last) + 2
        for v in dirs.get("note", []):
            c = ws.cell(row=nrow, column=1, value=v)
            c.font = Font(italic=True, color="666666")
            c.data_type = "s"
            nrow += 1
        for v in dirs.get("chart", []):
            charts.append((s, ncols, last, v))
        # further tables on the same sheet (`### Block: Title | at=H2`)
        if len(s["blocks"]) > MAX_BLOCKS:
            _warn(f"sheet '{s['name']}': only the first {MAX_BLOCKS} blocks are placed")
        below = max(nrow, (total_row or last) + 2)
        for b in s["blocks"][:MAX_BLOCKS]:
            if not b["header"]:
                _warn(f"sheet '{s['name']}': block '{b['title']}' has no table")
                continue
            below = _block(ws, b, s["name"], below, accent, band, names, formulas, Font)

    # defined names
    from openpyxl.workbook.defined_name import DefinedName
    for v in spec["names"]:
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_.]*)\s*=\s*(.+)$", v.strip())
        if not m:
            _warn(f"name '{v}': expects `Name = Sheet!B2`")
            continue
        ref = m.group(2).strip()
        sh = re.match(r"^'?(.+?)'?!", ref)
        if not sh or sh.group(1).lower() not in by_name:
            _warn(f"name '{m.group(1)}': must point at a cell on a sheet of this workbook (got '{ref}')")
            continue
        cells = ref[sh.end():].replace("$", "")
        if not re.match(r"^[A-Za-z]{1,3}\d{1,7}(:[A-Za-z]{1,3}\d{1,7})?$", cells):
            _warn(f"name '{m.group(1)}': must be a cell or range such as Sheet!B2 or Sheet!A2:A50 (got '{ref}')")
            continue
        # absolute cells: a name written as Sheet!B2 is RELATIVE in Excel and LibreOffice (it points somewhere else from every
        # cell that uses it, usually at an empty cell, so =C2*TaxRate quietly gives 0)
        absolute = re.sub(r"([A-Za-z]{1,3})(\d{1,7})", lambda mm: f"${mm.group(1).upper()}${mm.group(2)}", cells)
        ref = ref[:sh.end()] + absolute
        ref = ref.replace(sh.group(1), by_name[sh.group(1).lower()]["xname"], 1)
        if not ref.startswith("'") and re.search(r"[^A-Za-z0-9_.]", by_name[sh.group(1).lower()]["xname"]):
            ref = "'" + ref.replace("!", "'!", 1)
        wb.defined_names[m.group(1)] = DefinedName(m.group(1), attr_text=ref)

    _add_charts(wb, charts, by_name, accent, bool(spec.get("auto")))
    wb.properties.title = spec["title"] or title
    wb.properties.creator = "Local AI platform"
    from openpyxl.workbook.properties import CalcProperties
    wb.calculation = CalcProperties(fullCalcOnLoad=True)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _text_width(text) -> int:
    """Display width of a cell's text in column units. len() under-sizes Bengali / Hindi (letters are wider than Latin,
    vowel signs add little) and CJK (double width), so those columns came out too narrow and the text was cut off."""
    import unicodedata
    w = 0.0
    for ch in str(text):
        o = ord(ch)
        if unicodedata.category(ch).startswith("M"):
            w += 0.35                                   # vowel signs / virama: a little extra, not a full cell
        elif 0x0900 <= o <= 0x0DFF:
            w += 1.35                                   # Indic scripts (Bengali 0980-09FF, Devanagari 0900-097F ...)
        elif unicodedata.east_asian_width(ch) in ("W", "F"):
            w += 2.0
        else:
            w += 1.0
    return int(w + 0.999)


def _format_room(number_format, digits: int) -> int:
    """Extra characters a number format adds to a value (thousands separators, decimals, currency sign), so a money
    column is wide enough and never shows ####."""
    nf = str(number_format or "")
    if "#,##0" not in nf and "0.00" not in nf:
        return 0
    return digits // 3 + (3 if ".00" in nf else 0) + (2 if '"' in nf else 0) + 1


def _block(ws, b: dict, sheet: str, below: int, accent: str, band: str, names: list, formulas: bool, Font) -> int:
    """Write one extra table on a sheet: a title row, a header row, the rows, an optional total. Placed at its `at=` cell,
    else under what is already on the sheet. Formulas in it use real cell addresses. -> the next free row below it."""
    from openpyxl.styles import Alignment, PatternFill
    from openpyxl.utils.cell import coordinate_from_string, column_index_from_string
    if b["at"]:
        col_s, r0 = coordinate_from_string(b["at"])
        c0 = column_index_from_string(col_s)
    else:
        r0, c0 = below, 1
    header = [h or f"Column{i + 1}" for i, h in enumerate(b["header"][:MAX_COLS])]
    ncols = len(header)
    rows = [(r + [""] * ncols)[:ncols] for r in b["rows"]]
    dirs = {}
    for k, v in b["directives"]:
        dirs.setdefault(k, []).append(v)
    fmts = {}
    for v in dirs.get("format", []):
        for part in re.split(r"\s*,\s*(?=[^,=]+=)", v):
            if "=" in part:
                k, f = part.split("=", 1)
                fmts[k.strip().lower()] = f.strip().lower()
    t = ws.cell(row=r0, column=c0, value=b["title"])
    t.data_type = "s"
    t.font = Font(bold=True, size=12, color=accent)
    hr = r0 + 1
    for i, h in enumerate(header):
        c = ws.cell(row=hr, column=c0 + i, value=h)
        c.data_type = "s"
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor=accent)
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for ri, row in enumerate(rows, hr + 1):
        for i, raw in enumerate(row):
            kind = fmts.get(header[i].lower()) or _infer([r[i] for r in rows])
            where = f"sheet '{sheet}' block '{b['title']}' cell {col_letter(c0 + i)}{ri}"
            val, is_formula = _cell_value(raw, kind, names, where, formulas)
            c = ws.cell(row=ri, column=c0 + i)
            if val is not None:
                c.value = val
                if isinstance(val, str) and not is_formula:
                    c.data_type = "s"
            nf = _named_format(kind) if kind else None
            if nf and (is_formula or isinstance(val, (int, float)) or hasattr(val, "isoformat")):
                c.number_format = nf
            elif isinstance(val, float) and isinstance(raw, str) and _PCT.match(raw.strip()):
                c.number_format = "0.0%"
            if (ri - hr) % 2 == 0:
                c.fill = PatternFill("solid", fgColor=band)
    if b.get("legend"):                          # a colour legend: the first cell of a row is filled with the colour it names
        for ri, row in enumerate(rows, hr + 1):
            bg, fg = _FILLS.get(str(row[0]).lower(), ("FFFFFF", "000000"))
            c = ws.cell(row=ri, column=c0)
            c.fill = PatternFill("solid", fgColor=bg)
            c.font = Font(bold=True, color=fg)
    last = hr + len(rows)
    end = last
    codes = {"sum": 109, "avg": 101, "average": 101, "min": 105, "max": 104, "count": 103}
    totals = {}
    for v in dirs.get("total", []):
        for k, fn in _kv_pairs(v).items():
            totals[k.strip().lower()] = fn.strip().lower()
    if totals and rows:
        end = last + 1
        ws.cell(row=end, column=c0, value="Total").font = Font(bold=True)
        for i, h in enumerate(header):
            fn = totals.get(h.lower())
            if fn in codes and i > 0:
                L = col_letter(c0 + i)
                c = ws.cell(row=end, column=c0 + i, value=f"=SUBTOTAL({codes[fn]},{L}{hr + 1}:{L}{last})")
                c.font = Font(bold=True)
                nf = _named_format(fmts.get(h.lower()) or "") if fmts.get(h.lower()) else None
                if nf:
                    c.number_format = nf
            elif fn and fn not in codes:
                _warn(f"sheet '{sheet}' block '{b['title']}': total '{fn}' is not known (sum, avg, min, max, count)")
    for i, h in enumerate(header):
        L = col_letter(c0 + i)
        longest = max([_text_width(h)] + [min(_text_width(r[i]), 40) if not str(r[i]).startswith("=") else 12 for r in rows] or [8])
        kind = fmts.get(h.lower()) or _infer([r[i] for r in rows])
        longest += _format_room(_named_format(kind) if kind else None, longest)
        ws.column_dimensions[L].width = max(ws.column_dimensions[L].width or 0, min(max(longest + 3, 9), 60))
    return max(below, end + 2)


_DASH_COLS = 16                 # B..Q: four KPI tiles of 3 columns with a gap, or two charts side by side
_TILE_STARTS = (2, 6, 10, 14)   # first column of each tile (B, F, J, N)


def _dashboard(ws, s: dict, spec: dict, accent: str, band: str, names: list, formulas: bool) -> list:
    """Draw a dashboard sheet: title banner, KPI tiles (label over a big number), then charts in a two-column grid.
    -> the sheet's chart entries for _add_charts, each with its `at=` cell set."""
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = 2.5
    for ci in range(2, 2 + _DASH_COLS):
        ws.column_dimensions[col_letter(ci)].width = 11.5
    dirs = {}
    for k, v in s["directives"]:
        dirs.setdefault(k, []).append(v)
    title = (dirs.get("title") or [spec["title"] or s["name"]])[0]
    ws.merge_cells(start_row=2, start_column=2, end_row=2, end_column=1 + _DASH_COLS)
    c = ws.cell(row=2, column=2, value=title)
    c.data_type = "s"
    c.font = Font(bold=True, size=18, color="FFFFFF")
    c.fill = PatternFill("solid", fgColor=accent)
    c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws.row_dimensions[2].height = 34
    edge = Side(style="thin", color="D0D5DD")
    row = 4
    kpis = dirs.get("kpi", [])
    if len(kpis) > MAX_KPIS:
        _warn(f"dashboard '{s['name']}': only the first {MAX_KPIS} of {len(kpis)} kpi lines are shown")
    for i, v in enumerate(kpis[:MAX_KPIS]):
        parts = [p.strip() for p in v.split("|")]
        m = re.match(r"^(.+?)\s*=\s*(.+)$", parts[0])
        if not m:
            _warn(f"dashboard '{s['name']}': kpi '{v}': expects `kpi: Label = =SUM(Sheet!C2:C9) | format=currency:BDT`")
            continue
        label, raw = m.group(1).strip(), m.group(2).strip()
        fmt = next((p.split("=", 1)[1].strip() for p in parts[1:] if p.lower().startswith("format=")), "")
        r0 = row + (i // 4) * 5
        c0 = _TILE_STARTS[i % 4]
        val, is_formula = _cell_value(raw, fmt.lower() or None, names, f"dashboard '{s['name']}' kpi '{label}'", formulas)
        for r in range(r0, r0 + 4):
            for cc in range(c0, c0 + 3):
                cell = ws.cell(row=r, column=cc)
                cell.fill = PatternFill("solid", fgColor=band)
                cell.border = Border(top=edge if r == r0 else None, bottom=edge if r == r0 + 3 else None,
                                     left=edge if cc == c0 else None, right=edge if cc == c0 + 2 else None)
        ws.merge_cells(start_row=r0, start_column=c0, end_row=r0, end_column=c0 + 2)
        lab = ws.cell(row=r0, column=c0, value=label)
        lab.data_type = "s"
        lab.font = Font(size=10, color="5F6B7A", bold=True)
        lab.alignment = Alignment(horizontal="left", vertical="center", indent=1)
        ws.merge_cells(start_row=r0 + 1, start_column=c0, end_row=r0 + 3, end_column=c0 + 2)
        big = ws.cell(row=r0 + 1, column=c0)
        big.value = val
        if isinstance(val, str) and not is_formula:
            big.data_type = "s"
        nf = (_named_format(fmt) or (fmt if spec.get("auto") and re.fullmatch(_FMT_RAW_AUTO, fmt) else None)) if fmt else None
        if nf:
            big.number_format = nf
        elif is_formula or isinstance(val, (int, float)):
            big.number_format = "#,##0.##"
        big.font = Font(size=22, bold=True, color=accent)
        big.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    shown = min(len(kpis), MAX_KPIS)
    row += 5 * (-(-shown // 4)) if shown else 0
    out = []
    for i, v in enumerate(dirs.get("chart", [])):
        if "source=" not in v.lower():
            _warn(f"dashboard '{s['name']}': chart '{v}' needs `source=<sheet with the data>`")
            continue
        at = f"{col_letter(2 if i % 2 == 0 else 10)}{row + (i // 2) * 17}"
        out.append((s, 0, 0, v + (f" | at={at}" if "at=" not in v.lower() else "") + " | w=18.5 | h=8"))
    row += 17 * (-(-len(out) // 2)) + (1 if out else 0)
    for v in dirs.get("note", []):
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=1 + _DASH_COLS)
        n = ws.cell(row=row, column=2, value=v)
        n.data_type = "s"
        n.font = Font(italic=True, color="666666")
        row += 1
    ws.sheet_properties.tabColor = accent
    # prints (and previews) as one landscape page wide, so tiles and charts are never cut at a page edge
    from openpyxl.worksheet.properties import PageSetupProperties
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    ws.print_area = f"A1:{col_letter(2 + _DASH_COLS)}{max(row, 6)}"
    return out


def _chart_palette(accent: str, n: int = 8) -> list:
    """Series colours from the workbook accent: the accent itself, then lighter / darker / hue-shifted relatives, so a
    multi-series chart or a pie looks like part of the workbook instead of openpyxl's default rainbow."""
    import colorsys
    a = (accent or "1F4E79").lstrip("#").upper()
    if len(a) != 6:
        a = "1F4E79"
    r, g, b = (int(a[i:i + 2], 16) / 255 for i in (0, 2, 4))
    h, l, sat = colorsys.rgb_to_hls(r, g, b)
    out = [a]
    for dh, dl in ((0.0, 0.2), (0.0, -0.16), (0.08, 0.0), (-0.08, 0.0), (0.5, 0.0), (0.0, 0.34), (0.16, -0.08), (-0.16, 0.08)):
        rr, gg, bb = colorsys.hls_to_rgb((h + dh) % 1.0, min(0.82, max(0.2, l + dl)), min(1.0, max(0.3, sat)))
        c = "%02X%02X%02X" % (round(rr * 255), round(gg * 255), round(bb * 255))
        if c not in out:
            out.append(c)
    return out[:max(1, n)]


def _axis_format(src: dict, header: str) -> str:
    """Number format of a chart's value axis: the y column's own format, trimmed to what an axis label can carry."""
    f = ""
    for k, v in reversed(src["directives"]):
        if k == "format":
            for part in re.split(r"\s*,\s*(?=[^,=]+=)", v):
                if "=" in part and part.split("=", 1)[0].strip().lower() == header.lower():
                    f = part.split("=", 1)[1].strip()
                    break
            if f:
                break
    if not f:
        ci = next((i for i, h in enumerate(src["header"]) if str(h).lower() == header.lower()), None)
        kind = _infer([r[ci] for r in src["rows"] if ci is not None and ci < len(r)]) if ci is not None else None
        f = _named_format(kind) if kind else ""
    else:
        f = _named_format(f) or f
    f = (f or "").split(";")[0]
    if "%" in f:
        return '0"%"' if '"%"' in f else "0%"
    if "0.0" in f and ".00" not in f:
        return "#,##0.0"
    return "#,##0" if f else "General"


def _txpr(size: int, color: str = "595959"):
    """Text properties for a chart's axis labels / legend: one typeface, a given size and colour."""
    from openpyxl.chart.text import RichText
    from openpyxl.drawing.text import CharacterProperties, Font as DFont, Paragraph, ParagraphProperties
    cp = CharacterProperties(sz=size * 100, solidFill=color, latin=DFont(typeface=FONT_NAME))
    return RichText(p=[Paragraph(pPr=ParagraphProperties(defRPr=cp), endParaRPr=cp)])


def _styled_title(text: str, color: str, size: int = 12, bold: bool = True):
    from openpyxl.chart.text import RichText, Text
    from openpyxl.chart.title import Title
    from openpyxl.drawing.text import CharacterProperties, Font as DFont, Paragraph, ParagraphProperties, RegularTextRun
    cp = CharacterProperties(sz=size * 100, b=bold, solidFill=color, latin=DFont(typeface=FONT_NAME))
    para = Paragraph(pPr=ParagraphProperties(defRPr=cp), r=[RegularTextRun(rPr=cp, t=text)])
    return Title(tx=Text(rich=RichText(p=[para])), overlay=False)


def _polish_chart(ch, kind: str, accent: str, title, src: dict, ycols: list, header: list, flags: set, xt, yt, yfmt=None) -> None:
    """The look of an auto-designed workbook's charts: framed white chart area, no plot border, light horizontal gridlines,
    small grey axis text, a title in the accent colour, axis numbers in the y column's format, markers on lines. A failure to
    style never loses the chart."""
    from openpyxl.chart.shapes import GraphicalProperties
    from openpyxl.drawing.line import LineProperties
    try:
        ch.graphical_properties = GraphicalProperties(solidFill="FFFFFF", ln=LineProperties(solidFill="D9E2ED"))
        ch.plot_area.graphicalProperties = GraphicalProperties(solidFill="FFFFFF", ln=LineProperties(noFill=True))
        if title:
            ch.title = _styled_title(str(title), accent)
        if kind in ("pie", "doughnut"):
            if ch.legend is not None:
                ch.legend.position = "r"
                ch.legend.txPr = _txpr(9)
            return
        if ch.legend is not None:
            ch.legend.position = "b"
            ch.legend.txPr = _txpr(9)
        for ax, text in ((ch.x_axis, xt), (ch.y_axis, yt)):
            ax.txPr = _txpr(9)
            ax.spPr = GraphicalProperties(ln=LineProperties(solidFill="BFC7D1"))
            ax.title = _styled_title(str(text), "595959", 10, False) if text else None
        ch.x_axis.majorGridlines = None
        if ch.y_axis.majorGridlines is not None:
            ch.y_axis.majorGridlines.spPr = GraphicalProperties(ln=LineProperties(solidFill="E9EEF4"))
        if kind != "scatter" and ycols:
            ch.y_axis.number_format = yfmt or _axis_format(src, header[ycols[0] - 1])
        if getattr(ch, "type", "") == "bar" and "horizontal" in flags:          # first row on top, value axis at the bottom
            ch.x_axis.scaling.orientation = "maxMin"
            ch.y_axis.crosses = "max"
        if kind == "line":
            for se in ch.series:
                se.marker.symbol = "circle"
                se.marker.size = 5
                col = se.graphicalProperties.line.solidFill
                rgb = getattr(getattr(col, "srgbClr", None), "val", None)
                if rgb:
                    se.marker.graphicalProperties = GraphicalProperties(solidFill=rgb, ln=LineProperties(solidFill=rgb))
    except Exception:
        pass


def _add_charts(wb, charts: list, by_name: dict, accent: str, auto: bool = False) -> None:
    if not charts:
        return
    from openpyxl.chart import AreaChart, BarChart, DoughnutChart, LineChart, PieChart, Reference, ScatterChart, Series
    placed = {}
    for s, ncols, last, v in charts:
        parts = [p.strip() for p in v.split("|")]
        kind = (parts[0] or "bar").lower()
        opts = {}
        flags = set()
        for p in parts[1:]:
            if "=" in p:
                k, val = p.split("=", 1)
                opts[k.strip().lower()] = val.strip()
            elif p:
                flags.add(p.lower())
        src = by_name.get(opts.get("source", s["name"]).lower())
        if src is None or not src.get("header"):
            _warn(f"chart: source sheet '{opts.get('source')}' does not exist or has no table")
            continue
        sws = wb[src["xname"]]
        header = [h or f"Column{i + 1}" for i, h in enumerate(src["header"][:MAX_COLS])]
        hdr = src.get("hdr", 1)                 # table header row (below any banner), set when the sheet was built
        nlast = len(src["rows"]) + hdr
        rowmode = opts.get("by", "").lower() == "row" and kind in ("bar", "column", "col", "line", "area")
        if rowmode:
            # `by=row | cols=FY2021:FY2025 | rows=Net profit,Assets`: one series per metric ROW, the period headers are the categories
            c_a, _, c_b = opts.get("cols", "").partition(":")
            hl = [h.lower() for h in header]
            c1 = hl.index(c_a.strip().lower()) + 1 if c_a.strip().lower() in hl else None
            c2 = hl.index(c_b.strip().lower()) + 1 if c_b.strip().lower() in hl else None
            first = [str(r[0]).strip().lower() if r else "" for r in src["rows"]]
            rws, bad = [], []
            for lab in [x.strip() for x in opts.get("rows", "").split(",") if x.strip()]:
                (rws if lab.lower() in first else bad).append(lab)
            if c1 is None or c2 is None or c2 < c1 or not rws:
                _warn(f"chart: by=row needs cols=<first period>:<last period> and rows=<metric names> that exist on sheet '{src['name']}'"
                      + _cols_hint(header))
                continue
            for lab in bad:
                _warn(f"chart: row '{lab}' not found in the first column of sheet '{src['name']}'")
            xcol, ycols = c1, [hdr + 1 + first.index(lab.lower()) for lab in rws]
        else:
            xcol = next((i for i, h in enumerate(header, 1) if h.lower() == opts.get("x", header[0]).lower()), None)
            if xcol is None:
                _warn(f"chart: x column '{opts.get('x')}' not found on sheet '{src['name']}'" + _cols_hint(header))
                continue
            ys = [y.strip() for y in opts.get("y", "").split(",") if y.strip()] or [header[1] if len(header) > 1 else header[0]]
            ycols = []
            for y in ys:
                yi = next((i for i, h in enumerate(header, 1) if h.lower() == y.lower()), None)
                if yi is None:
                    _warn(f"chart: y column '{y}' not found on sheet '{src['name']}'" + _cols_hint(header))
                    ycols = []
                    break
                ycols.append(yi)
        if not ycols:
            continue
        if kind in ("bar", "column", "col"):
            ch = BarChart()
            ch.type = "bar" if kind == "bar" and "horizontal" in flags else "col"
            if "stacked" in flags:
                ch.grouping, ch.overlap = "stacked", 100
        elif kind == "line":
            ch = LineChart()
        elif kind == "area":
            ch = AreaChart()
        elif kind == "pie":
            ch = PieChart()
        elif kind == "doughnut":
            ch = DoughnutChart()
            ch.holeSize = 55
        elif kind == "scatter":
            ch = ScatterChart()
        else:
            _warn(f"chart: type '{kind}' not supported (bar, column, line, area, pie, doughnut, scatter)")
            continue
        ch.title = opts.get("title") or None
        ch.width, ch.height = float(opts.get("w", 16)), float(opts.get("h", 8.5))
        cats = Reference(sws, min_col=xcol, min_row=hdr + 1, max_row=nlast)
        if rowmode:
            from openpyxl.chart.data_source import StrRef
            from openpyxl.chart.series import SeriesLabel
            for r in ycols:
                se = Series(Reference(sws, min_col=c1, max_col=c2, min_row=r))
                se.tx = SeriesLabel(strRef=StrRef(f=str(Reference(sws, min_col=1, min_row=r))))
                ch.series.append(se)
            from openpyxl.chart.data_source import AxDataSource
            for se in ch.series:                                 # the period headers are text: a string reference
                se.cat = AxDataSource(strRef=StrRef(f=str(Reference(sws, min_col=c1, max_col=c2, min_row=hdr))))
        elif kind == "scatter":
            for yi in ycols:
                se = Series(Reference(sws, min_col=yi, min_row=hdr, max_row=nlast), Reference(sws, min_col=xcol, min_row=hdr + 1, max_row=nlast),
                            title_from_data=True)
                se.marker.symbol = "circle"
                se.graphicalProperties.line.noFill = True
                ch.series.append(se)
        else:
            for yi in ycols:
                ch.add_data(Reference(sws, min_col=yi, min_row=hdr, max_row=nlast), titles_from_data=True)
            ch.set_categories(cats)
        xt = opts.get("xlabel") or (None if rowmode else header[xcol - 1])
        yt = opts.get("ylabel") or (None if rowmode else header[ycols[0] - 1] if len(ycols) == 1 else None)
        if kind not in ("pie", "doughnut"):
            ch.x_axis.title, ch.y_axis.title = xt, yt
            ch.x_axis.delete = False
            ch.y_axis.delete = False
        if len(ycols) == 1 and kind in ("bar", "column", "col", "line", "area"):
            ch.legend = None
        pal = _chart_palette(accent, 8)
        if kind in ("pie", "doughnut") and ch.series:
            from openpyxl.chart.series import DataPoint
            for i in range(min(len(src["rows"]), 12)):
                pt = DataPoint(idx=i)
                pt.graphicalProperties.solidFill = pal[i % len(pal)]
                ch.series[0].dPt.append(pt)
        else:
            for i, se in enumerate(ch.series):
                c = pal[i % len(pal)]
                if kind in ("line", "scatter"):
                    se.graphicalProperties.line.solidFill = c
                    se.graphicalProperties.line.width = 28575          # 2.25 pt
                    se.smooth = False
                    if kind == "scatter":
                        se.marker.graphicalProperties = None
                else:
                    se.graphicalProperties.solidFill = c
        if kind in ("bar", "column", "col"):
            ch.gapWidth = 60
        try:                                                           # no border around the chart area
            from openpyxl.chart.shapes import GraphicalProperties
            ch.graphical_properties = GraphicalProperties()
            ch.graphical_properties.line.noFill = True
        except Exception:
            pass
        if "labels" in flags or (kind in ("bar", "column", "col", "pie", "doughnut") and len(src["rows"]) <= 12 and len(ycols) == 1):
            from openpyxl.chart.label import DataLabelList
            ch.dataLabels = DataLabelList()
            ch.dataLabels.showVal = kind not in ("pie", "doughnut")
            ch.dataLabels.showPercent = kind in ("pie", "doughnut")
            for flag in ("showSerName", "showCatName", "showLegendKey"):
                setattr(ch.dataLabels, flag, False)
        if auto:
            _polish_chart(ch, kind, accent, opts.get("title"), src, ycols, header, flags, xt, yt, opts.get("yfmt") or ("#,##0" if rowmode else None))
        target = wb[s["xname"]]
        at = opts.get("at")
        if not at:
            n = placed.get(s["xname"], 0)
            at = f"{col_letter(ncols + 2)}{(s.get('hdr', 1) if auto else 2) + n * 18}"
            placed[s["xname"]] = n + 1
        target.add_chart(ch, at.upper())
