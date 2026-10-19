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

Everything after the first table of a sheet that looks like `key: value` is a directive (see DIRECTIVES). Formulas are checked by
core.sheet_engine.formulas before they are written; anything refused raises BuildError with the cell and the reason.
"""
import contextvars
import io
import re
from typing import Optional

from .formulas import FormulaError, check_formula, to_file_form

MAX_SHEETS = 20
MAX_COLS = 100
MAX_ROWS_TOTAL = 50_000

DIRECTIVES = ("format", "total", "freeze", "filter", "highlight", "scale", "databar", "validate", "table", "name", "chart",
              "widths", "note", "print", "tab", "wrap", "hide", "merge", "color", "align", "border")
_DIRECTIVE_RX = re.compile(r"^\s*(" + "|".join(DIRECTIVES) + r")\s*:\s*(.*?)\s*$", re.I)
_SHEET_RX = re.compile(r"^\s*##\s*sheet\s*:\s*(.+?)\s*$", re.I)
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


def _cols_hint(header: list) -> str:
    return f" (its columns: {', '.join(str(h) for h in header[:12])})"


# ---- parsing ---------------------------------------------------------------------------------------------------------
def is_outline(text: str) -> bool:
    """A workbook outline has `## Sheet:` headings, or a table with directive lines (format:, chart:, total: ...)."""
    t = str(text or "")
    if re.search(r"(?mi)^\s*##\s*sheet\s*:", t):
        return True
    return bool(re.search(r"(?m)^\s*\|", t)) and bool(re.search(r"(?mi)^\s*(format|total|chart|highlight|scale|databar|validate|table|name|freeze|widths|merge|color|align|border)\s*:", t))


def _split_row(line: str) -> list:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.replace("\\|", "|").strip() for c in re.split(r"(?<!\\)\|", s)]


def _is_sep(cells: list) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c.strip()) or not c.strip() for c in cells)


def parse(text: str) -> dict:
    wb = {"title": "", "theme": DEFAULT_THEME, "sheets": [], "names": []}
    cur: Optional[dict] = None

    def start(name):
        nonlocal cur
        cur = {"name": name, "header": None, "rows": [], "directives": []}
        wb["sheets"].append(cur)
    for raw in str(text or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        m = _SHEET_RX.match(line)
        if m:
            start(m.group(1))
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
        if line.lstrip().startswith("|"):
            if cur is None:
                start("Sheet1")
            cells = _split_row(line)
            if _is_sep(cells):
                continue
            if cur["header"] is None:
                cur["header"] = cells
            else:
                cur["rows"].append(cells)
            continue
        d = _DIRECTIVE_RX.match(line)
        if d:
            key, val = d.group(1).lower(), d.group(2)
            if key == "name":
                wb["names"].append(val)
            else:
                if cur is None:
                    start("Sheet1")
                cur["directives"].append((key, val))
    wb["sheets"] = [s for s in wb["sheets"] if s["header"]]
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
        return float(t.rstrip("% ").replace(" ", "")) / 100
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
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    _warnings.set([])
    spec = parse(text)
    if not spec["sheets"]:
        raise BuildError("no sheet with a table found - write `## Sheet: Name` followed by a markdown table")
    if len(spec["sheets"]) > MAX_SHEETS:
        raise BuildError(f"at most {MAX_SHEETS} sheets per workbook")
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
        header = [h or f"Column{i + 1}" for i, h in enumerate(s["header"][:MAX_COLS])]
        ncols = len(header)
        rows = [(r + [""] * ncols)[:ncols] for r in s["rows"]]
        dirs = {}
        for k, v in s["directives"]:
            dirs.setdefault(k, []).append(v)
        fmt_map = {}
        for v in dirs.get("format", []):
            for part in re.split(r"\s*,\s*(?=[^,=]+=)", v):
                if "=" in part:
                    k, f = part.split("=", 1)
                    fmt_map[k.strip().lower()] = _named_format(f) or (f.strip() if re.fullmatch(r"[#0,.\-%$€£₹৳ \"A-Za-z/:@_()]+", f.strip()) else None)
                    fmt_map.setdefault("__kind__" + k.strip().lower(), f.strip().lower())
        col_fmt = []
        for ci, h in enumerate(header):
            col_vals = [r[ci] for r in rows]
            explicit = fmt_map.get(h.lower())
            kind = fmt_map.get("__kind__" + h.lower()) or _infer(col_vals)
            col_fmt.append((kind, explicit or (_named_format(kind) if kind else None)))

        # header
        for ci, h in enumerate(header, 1):
            c = ws.cell(row=1, column=ci, value=h)
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor=accent)
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        # body
        for ri, row in enumerate(rows, 2):
            for ci, raw in enumerate(row, 1):
                kind, nf = col_fmt[ci - 1]
                where = f"sheet '{s['name']}' cell {col_letter(ci)}{ri}"
                val, is_formula = _cell_value(raw, kind, names, where, formulas)
                c = ws.cell(row=ri, column=ci)
                if is_formula:
                    c.value = val
                elif val is not None:
                    c.value = val
                    if isinstance(val, str):
                        c.data_type = "s"          # plain text never becomes a formula
                if nf and (is_formula or isinstance(val, (int, float)) or hasattr(val, "isoformat")):
                    c.number_format = nf
                c.border = Border(bottom=thin)
                if isinstance(val, (int, float)) or is_formula:
                    c.alignment = Alignment(horizontal="right", vertical="top")
                elif isinstance(val, str) and len(val) > 45:
                    c.alignment = Alignment(wrap_text=True, vertical="top")      # long text wraps instead of running off the sheet
                else:
                    c.alignment = Alignment(vertical="top")
        last = len(rows) + 1

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
                cell = ws.cell(row=total_row, column=idx, value=f"=SUBTOTAL({codes[fn.lower()]},{col_letter(idx)}2:{col_letter(idx)}{last})")
                cell.font = Font(bold=True)
                if col_fmt[idx - 1][1]:
                    cell.number_format = col_fmt[idx - 1][1]
                cell.alignment = Alignment(horizontal="right")
            for ci in range(1, ncols + 1):
                ws.cell(row=total_row, column=ci).border = Border(top=Side(style="medium", color=accent))

        # banding (a real Excel table brings its own)
        table_name = (dirs.get("table") or [""])[0].strip()
        if not table_name:
            for ri in range(3, last + 1, 2):
                for ci in range(1, ncols + 1):
                    ws.cell(row=ri, column=ci).fill = PatternFill("solid", fgColor=band)

        # widths
        for ci, h in enumerate(header, 1):
            longest = max([len(str(h))] + [min(len(str(r[ci - 1])), 40) if not str(r[ci - 1]).startswith("=") else 12 for r in rows] or [8])
            ws.column_dimensions[col_letter(ci)].width = min(max(longest + 3, 9), 60)
        for v in dirs.get("widths", []):
            for k, w in _kv_pairs(v).items():
                try:
                    ws.column_dimensions[k.upper()].width = float(w)
                except ValueError:
                    pass

        # freeze, filter, table, print
        freeze = (dirs.get("freeze") or ["A2"])[-1].strip()
        if freeze.lower() not in ("none", "off", "no"):
            ws.freeze_panes = freeze.upper() if re.fullmatch(r"[A-Za-z]{1,3}\d+", freeze) else "A2"
        flt = (dirs.get("filter") or ["on"])[-1].strip().lower()
        if table_name:
            from openpyxl.worksheet.table import Table, TableStyleInfo
            safe = re.sub(r"[^A-Za-z0-9_]", "_", table_name)
            if not re.match(r"[A-Za-z_]", safe):
                safe = "t_" + safe
            tb = Table(displayName=safe, ref=f"A1:{col_letter(ncols)}{last}")
            tb.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
            ws.add_table(tb)
        elif flt not in ("off", "no", "none") and rows:
            ws.auto_filter.ref = f"A1:{col_letter(ncols)}{last}"
        ws.page_setup.orientation = "landscape" if ncols > 6 else "portrait"
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
        ws.sheet_properties.pageSetUpPr = openpyxl.worksheet.properties.PageSetupProperties(fitToPage=True)
        ws.print_title_rows = "1:1"
        for v in dirs.get("tab", []):
            if re.fullmatch(r"[0-9A-Fa-f]{6}", v.strip()):
                ws.sheet_properties.tabColor = v.strip().upper()

        # conditional formatting
        from openpyxl.formatting.rule import CellIsRule, ColorScaleRule, DataBarRule
        rng = lambda ci: f"{col_letter(ci)}2:{col_letter(ci)}{max(last, 2)}"      # noqa: E731
        for v in dirs.get("highlight", []):
            m = re.match(r"^(?P<col>.+?)\s*(?P<op>>=|<=|!=|<>|=|>|<|is)\s*(?P<val>.+?)\s+(?P<color>red|green|amber|yellow|blue|grey|gray)$", v.strip(), re.I)
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
                fa = a_val if _to_number(a_val) is not None else f'"{a_val}"'
                fb = b_val if _to_number(b_val) is not None else f'"{b_val}"'
                ws.conditional_formatting.add(rng(idx), CellIsRule(operator="between", formula=[fa, fb],
                                              fill=PatternFill("solid", bgColor=bg, fgColor=bg), font=Font(color=fg)))
            else:
                op_raw = m.group("op").lower()
                op = {"=": "equal", "is": "equal", ">": "greaterThan", "<": "lessThan",
                      ">=": "greaterThanOrEqual", "<=": "lessThanOrEqual", "!=": "notEqual", "<>": "notEqual"}[op_raw]
                val = m.group("val").strip().strip("'\"")
                formula = val if _to_number(val) is not None else f'"{val}"'
                ws.conditional_formatting.add(rng(idx), CellIsRule(operator=op, formula=[formula],
                                              fill=PatternFill("solid", bgColor=bg, fgColor=bg), font=Font(color=fg)))
        for key in ("scale", "databar"):
            for v in dirs.get(key, []):
                reverse = v.strip().lower().endswith(" reverse")
                cname = re.sub(r"\s+reverse$", "", v.strip(), flags=re.I)
                idx = next((i for i, h in enumerate(header, 1) if h.lower() == cname.lower()), None)
                if idx is None:
                    _warn(f"sheet '{s['name']}': {key}: column '{cname}' not found" + _cols_hint(header))
                    continue
                if key == "scale":
                    lo, mid, hi = ("63BE7B", "FFEB84", "F8696B") if reverse else ("F8696B", "FFEB84", "63BE7B")
                    ws.conditional_formatting.add(rng(idx), ColorScaleRule(start_type="min", start_color=lo, mid_type="percentile",
                                                                          mid_value=50, mid_color=mid, end_type="max", end_color=hi))
                else:
                    ws.conditional_formatting.add(rng(idx), DataBarRule(start_type="min", end_type="max", color=accent))

        # data validation
        from openpyxl.worksheet.datavalidation import DataValidation
        for v in dirs.get("validate", []):
            m = re.match(r"^(?P<col>.+?)\s+in\s*\[(?P<items>.*)\]\s*$", v.strip(), re.I)
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
            ws.add_data_validation(dv)
            dv.add(f"{col_letter(idx)}2:{col_letter(idx)}{max(last + 200, 500)}")      # a few hundred rows of room to add data

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
                    for ri in range(2, last + 1):
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
                        for ri in range(2, last + 1):
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

        # notes under the table
        nrow = (total_row or last) + 2
        for v in dirs.get("note", []):
            c = ws.cell(row=nrow, column=1, value=v)
            c.font = Font(italic=True, color="666666")
            c.data_type = "s"
            nrow += 1
        for v in dirs.get("chart", []):
            charts.append((s, ncols, last, v))

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
        ref = ref.replace(sh.group(1), by_name[sh.group(1).lower()]["xname"], 1)
        if not ref.startswith("'") and re.search(r"[^A-Za-z0-9_.]", by_name[sh.group(1).lower()]["xname"]):
            ref = "'" + ref.replace("!", "'!", 1)
        wb.defined_names[m.group(1)] = DefinedName(m.group(1), attr_text=ref)

    _add_charts(wb, charts, by_name, accent)
    wb.properties.title = spec["title"] or title
    wb.properties.creator = "Local AI platform"
    from openpyxl.workbook.properties import CalcProperties
    wb.calculation = CalcProperties(fullCalcOnLoad=True)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _add_charts(wb, charts: list, by_name: dict, accent: str) -> None:
    if not charts:
        return
    from openpyxl.chart import AreaChart, BarChart, LineChart, PieChart, Reference, ScatterChart, Series
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
        if src is None:
            _warn(f"chart: source sheet '{opts.get('source')}' does not exist")
            continue
        sws = wb[src["xname"]]
        header = [h or f"Column{i + 1}" for i, h in enumerate(src["header"][:MAX_COLS])]
        nlast = len(src["rows"]) + 1
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
        elif kind == "scatter":
            ch = ScatterChart()
        else:
            _warn(f"chart: type '{kind}' not supported (bar, column, line, area, pie, scatter)")
            continue
        ch.title = opts.get("title") or None
        ch.width, ch.height = float(opts.get("w", 16)), float(opts.get("h", 8.5))
        cats = Reference(sws, min_col=xcol, min_row=2, max_row=nlast)
        if kind == "scatter":
            for yi in ycols:
                se = Series(Reference(sws, min_col=yi, min_row=1, max_row=nlast), Reference(sws, min_col=xcol, min_row=2, max_row=nlast),
                            title_from_data=True)
                se.marker.symbol = "circle"
                se.graphicalProperties.line.noFill = True
                ch.series.append(se)
        else:
            for yi in ycols:
                ch.add_data(Reference(sws, min_col=yi, min_row=1, max_row=nlast), titles_from_data=True)
            ch.set_categories(cats)
        if kind != "pie":
            ch.x_axis.title = opts.get("xlabel") or header[xcol - 1]
            ch.y_axis.title = opts.get("ylabel") or (header[ycols[0] - 1] if len(ycols) == 1 else None)
            ch.x_axis.delete = False
            ch.y_axis.delete = False
        if len(ycols) == 1 and kind in ("bar", "column", "col", "line", "area"):
            ch.legend = None
        if kind in ("bar", "column", "col") and len(ycols) == 1 and ch.series:
            ch.series[0].graphicalProperties.solidFill = accent
        target = wb[s["xname"]]
        at = opts.get("at")
        if not at:
            n = placed.get(s["xname"], 0)
            at = f"{col_letter(ncols + 2)}{2 + n * 18}"
            placed[s["xname"]] = n + 1
        target.add_chart(ch, at.upper())
