"""Auto-design: a deterministic pass that makes a parsed workbook look professional, driven by what its tables contain.

The model writes content, rarely design. `enrich(spec)` adds the design directives it left out (number formats, status colours,
drop-downs, data bars, trend icons, totals, section bands, banners) to the parsed spec, so the one renderer in build.py does the
drawing. It is not a template: every rule fires only when the content calls for it (a one-table export stays light), and an
explicit directive of the model for a column or sheet always wins.

Beyond the per-column design it adds, only when the data calls for it: charts on a sheet whose table is a period series, a
category/value list or a share breakdown; a Contents sheet (4+ data sheets); and a Dashboard of live KPI tiles (3+ data sheets with
3+ figures to show).

Off switches: `style: plain` (workbook line before the first sheet), `auto: off` (one sheet), config `sheet_engine.autodesign`.
"""
import re

from ..doc_ops.xlsx_ops.financial_styles import is_total_row
from .build import (MAX_COLS, MAX_SHEETS, SECTION_PREFIX, _CURRENCY, _ISO_DATE, _PCT, _infer, _plain, _safe_sheet_name, _to_number,
                    col_letter)

PLACEHOLDERS = {"", "n/a", "na", "npd", "-", "—", "–", "not disclosed", "tbd"}
_OFF = {"off", "no", "false", "0", "none", "plain"}

_PCT_WORDS = {"rate", "ratio", "margin", "share", "growth", "yoy", "roe", "roa", "npl", "car", "percent", "pct", "percentage"}
_PCT_LITERAL = {"percent", "pct", "percentage"}
_MONEY_WORDS = {"bdt", "usd", "tk", "cr", "mn", "bn", "revenue", "profit", "cost", "costs", "amount", "income", "expense", "expenses",
                "budget", "sales", "salary", "fee", "fees", "payment", "payments", "price", "spend", "loss"}
_SIGNED_WORDS = {"variance", "change", "delta", "growth", "yoy", "net", "profit", "gap", "diff", "difference", "loss"}
_ID_WORDS = {"id", "no", "number", "code", "phone", "mobile", "zip", "postal", "account", "sku", "ref", "invoice", "nid", "tin",
             "serial", "sl", "sn", "barcode", "isbn", "pin"}
_ADDITIVE = {"amount", "revenue", "sales", "cost", "costs", "expense", "expenses", "budget", "spend", "count", "units", "qty",
             "quantity", "total", "actual", "forecast", "variance", "profit", "income", "salary", "payment", "payments", "fee", "fees"}
_RATIO_WORDS = _PCT_WORDS | {"avg", "average", "per", "index", "score", "price"}
_STATUS_WORDS = {"status", "priority", "rag", "risk", "trend", "rating", "outcome", "health", "verified", "result", "stage"}
_ICON_WORDS = {"growth", "yoy", "change", "delta"}

_GREEN, _AMBER, _RED, _GREY = "green", "amber", "red", "grey"


def _colour_map(green: str, amber: str, red: str, grey: str = "") -> dict:
    return {v.strip(): c for c, vs in ((_GREEN, green), (_AMBER, amber), (_RED, red), (_GREY, grey)) for v in vs.split(",") if v.strip()}


_COMMON = _colour_map("green", "amber,yellow", "red", "unknown,n/a,na,not applicable")
_LEVEL = _colour_map("low,minor", "medium,moderate,med", "high,critical,severe,urgent")
_RATING = _colour_map("high,excellent,good,strong,outstanding,very good", "medium,average,fair,moderate,satisfactory",
                      "low,poor,weak,bad,very poor")
_TREND = _colour_map("up,rising,improving,positive,increasing,growing", "", "down,falling,declining,negative,decreasing,worsening",
                     "flat,stable,steady,unchanged,neutral")
_STATUS = _colour_map("verified,done,completed,complete,approved,on track,active,yes,pass,passed,paid,resolved,healthy,ok,delivered",
                      "reported,pending,in progress,medium,partial,planned,at risk,in review,scheduled,ongoing",
                      "not disclosed,failed,fail,delayed,blocked,no,rejected,overdue",
                      "estimated,unknown,n/a,na,on hold,tbd")


def acct_number(s: str, default=None):
    """`(1,234)` is the accounting way to write minus 1,234 -> -1234; anything else -> default."""
    m = re.match(r"^\(\s*(.+?)\s*\)$", str(s).strip())
    if m and not m.group(1).lstrip().startswith(("-", "+")):
        n = _to_number(m.group(1))
        if n is not None:
            return -n
    return default


def _num(raw: str):
    n = _to_number(raw)
    return n if n is not None else acct_number(raw)


def _tokens(h: str) -> list:
    return re.findall(r"[a-z]+", h.lower())


def _usable_header(h: str, header: list) -> bool:
    """A header the directive parsers can address by name: no separators, operators or keywords that would split `Col = v color`."""
    return bool(h) and not re.search(r"[,=<>!|\[\]]", h) and not re.search(r"(?i)\b(is|in|between|reverse)\b", h) \
        and [x.lower() for x in header].count(h.lower()) == 1


def _stats(vals: list) -> dict:
    """What a column holds, ignoring placeholders: numbers, text, formulas, percent strings, currency symbols, ISO dates."""
    st = {"nums": [], "text": 0, "formulas": 0, "pct": 0, "syms": [], "dec": 0, "iso": 0, "raws": []}
    for raw in vals:
        t = str(raw).strip()
        if t.lower() in PLACEHOLDERS:
            continue
        if t.startswith("="):
            st["formulas"] += 1
            continue
        if _ISO_DATE.match(t):
            st["iso"] += 1
        n = _num(t)
        if n is None:
            st["text"] += 1
            continue
        st["nums"].append(n)
        st["raws"].append(t)
        st["pct"] += bool(_PCT.match(t))
        m = _CURRENCY.match(t)
        if m:
            st["syms"].append(m.group("sym").upper().rstrip("."))
        d = re.search(r"\.(\d+)", t)
        st["dec"] = max(st["dec"], len(d.group(1)) if d else 0)
    return st


def _number_format(h: str, st: dict, money: bool, red: bool) -> str:
    """The number format a numeric column gets, or '' to leave it as it is."""
    toks, nums = _tokens(h), st["nums"]
    if h.strip().lower() in ("year", "fy", "fiscal year", "yr") or (
            nums and all(re.fullmatch(r"\d{4}", r) and 1900 <= int(r) <= 2100 for r in st["raws"]) and not money):
        return "0"
    if st["pct"]:
        if st["pct"] < len(nums):
            return ""                                        # percent strings among plain numbers: each cell keeps its own format
        base = "0.0%"
    elif "%" in h or set(toks) & _PCT_WORDS:
        fractions = nums and all(abs(v) <= 1.5 for v in nums) and any(float(v) != int(v) for v in nums)
        if fractions:
            base = "0.0%"
        elif "%" in h or set(toks) & _PCT_LITERAL:
            base = '0.0"%"'                                  # 23.5 under a % header is already percent: never divide it silently
        else:
            base = _plain_number(st, money)                  # a "Rate" of 110.25 is no percentage
    else:
        base = _plain_number(st, money)
    if not base:
        return ""
    return f"{base};[Red]-{base}" if red and base != "0" else base


def _plain_number(st: dict, money: bool) -> str:
    dec = 2 if money and st["dec"] >= 1 else st["dec"]
    return {0: "#,##0", 1: "#,##0.0", 2: "#,##0.00"}.get(dec, "")


def _covers(values: list, h: str) -> bool:
    """True when one of the model's own directive values starts with this column's name (`Status = Done green`, `Sales`)."""
    return any(re.match(r"^\s*" + re.escape(h) + r"(?!\w)", v, re.I) for v in values)


def _named_cols(values: list, sep: str) -> set:
    out = set()
    for v in values:
        for part in re.split(sep, v):
            if "=" in part:
                out.add(part.split("=", 1)[0].strip().lower())
    return out


def _unsafe_sheets(spec: dict) -> set:
    """Lower-case names of sheets whose rows must not move or grow: one that holds a formula (it points at cells by address) or
    that anything else names with `Sheet!` (formulas, kpi lines, defined names)."""
    strings, unsafe = list(spec.get("names", [])), set()
    for s in spec["sheets"]:
        cells = list(s["header"] or []) + [c for r in s["rows"] for c in r]
        strings += [v for _, v in s["directives"]]
        for b in s["blocks"]:
            cells += list(b["header"] or []) + [c for r in b["rows"] for c in r]
            strings += [v for _, v in b["directives"]]
        strings += cells
        if any(str(c).lstrip().startswith("=") for c in cells):
            unsafe.add(s["name"].lower())
    blob = "\n".join(str(x) for x in strings)
    for s in spec["sheets"]:
        for n in {s["name"], s.get("xname", s["name"])}:
            if re.search(r"(?i)(?:'" + re.escape(n) + r"'|(?<![\w.]))" + re.escape(n) + r"\s*!", blob):
                unsafe.add(s["name"].lower())
    return unsafe


_ADDR_RX = re.compile(r"\b[A-Za-z]{1,3}\d{1,7}\b")
_CHATTER = re.compile(r"(?i)^(here\b|below\b|the (table|following)\b|this (table|sheet)\b|table\b|sheet\b|data\b)")


def _bands(s: dict, ncols: int) -> int:
    """Rows with only their first cell filled become `>> Title` section bands (no row moves). -> how many."""
    rows = s["rows"]
    if ncols < 2 or len(rows) < 3 or any(str(r[0]).startswith(SECTION_PREFIX) for r in rows if r):
        return 0

    def cells(r):
        return [_plain(c).strip() for c in (list(r) + [""] * ncols)[:ncols]]
    lone = [i for i, r in enumerate(rows) if cells(r)[0] and not any(cells(r)[1:])]
    if not lone or len(lone) * 2 > len(rows):
        return 0                                             # a sparse list, not a table with headings
    made = 0
    for i in lone:
        first = cells(rows[i])[0]
        if (i == len(rows) - 1 or first.startswith("=") or is_total_row(first) or _num(first) is not None or len(first) > 60
                or sum(1 for c in cells(rows[i + 1]) if c) < 2):
            continue                                         # the heading must lead into rows that have data
        rows[i] = [SECTION_PREFIX + first] + list(rows[i][1:])
        made += 1
    return made


def _status_colours(h: str, vals: list):
    """-> ([(value, colour)], [distinct values]) for a status-like column, or None when it is not one."""
    toks = set(_tokens(h))
    if not toks & _STATUS_WORDS:
        return None
    seen = {}
    for v in vals:
        t = str(v).strip()
        if t:
            seen.setdefault(t.lower(), t)
    if not seen or len(seen) > 12:
        return None
    primary = (_LEVEL if toks & {"priority", "risk"} else _TREND if "trend" in toks
               else _RATING if "rating" in toks else _STATUS)
    rules = []
    for low, shown in seen.items():
        colour = primary.get(low) or _COMMON.get(low)
        if colour and not re.search(r"[\"'=<>!]", shown) and _num(shown) is None:
            rules.append((shown, colour))
    if len(rules) * 2 < len(seen):
        return [], list(seen.values())
    return rules, list(seen.values())


def _design_sheet(s: dict, spec: dict, unsafe: set, count: dict) -> None:
    header = [h or f"Column{i + 1}" for i, h in enumerate(s["header"][:MAX_COLS])]
    ncols = len(header)
    dirs = s["directives"]
    vals_of = lambda key: [v for k, v in dirs if k == key]      # noqa: E731
    count["band"] = count.get("band", 0) + _bands(s, ncols)
    body = [(list(r) + [""] * ncols)[:ncols] for r in s["rows"]
            if not (str(r[0]).startswith(SECTION_PREFIX) and not any(str(x).strip() for x in r[1:]))]
    explicit_fmt = _named_cols(vals_of("format"), r"\s*,\s*(?=[^,=]+=)")
    explicit_total = vals_of("total")
    visual_vals = vals_of("highlight") + vals_of("scale") + vals_of("databar") + vals_of("icons")
    safe = s["name"].lower() not in unsafe
    add, numcols, totals = [], set(), []
    for ci, h in enumerate(header):
        if not _usable_header(h, header):
            continue
        vals = [r[ci] for r in body]
        st = _stats(vals)
        lit = len(st["nums"]) + st["text"]
        toks = set(_tokens(h))
        money = bool(toks & _MONEY_WORDS or re.search(r"[$৳€£]", h))
        is_num = bool(st["nums"]) and len(st["nums"]) / lit >= 0.7
        if not is_num and not st["nums"] and st["formulas"] and not st["text"] and (money or toks & _ADDITIVE):
            is_num = True                                    # a column of formulas that is plainly money or a count
        if st["iso"] and st["iso"] / max(lit, 1) >= 0.7 and not is_num:
            if h.lower() not in explicit_fmt and _infer(vals) != "date":
                add.append(("format", f"{h}=date"))
                count["datefmt"] = count.get("datefmt", 0) + 1
            continue
        if _tokens(h)[-1:] and _tokens(h)[-1] in _ID_WORDS and not money:
            is_num = False                                   # an identifier (invoice no, phone): no separators, no total
        if is_num:
            numcols.add(h.lower())
            count["text"] = count.get("text", 0) + st["text"]
            year = bool(re.fullmatch(r"(?i)year|fy|fiscal year|yr", h.strip()))
            signed = bool(toks & _SIGNED_WORDS)
            fmt = ""
            if h.lower() not in explicit_fmt:
                inferred = _infer(vals)
                red = signed or any(v < 0 for v in st["nums"])
                if st["syms"] and len(st["syms"]) * 2 >= len(st["nums"]) and len(set(st["syms"])) == 1:
                    fmt = "" if (inferred or "").startswith("currency") else f"currency:{st['syms'][0]}"
                elif st["nums"]:
                    fmt = _number_format(h, st, money, red)
                else:
                    fmt = "#,##0.00"                         # formulas only: money by its header
                if inferred == "date" or (inferred or "").startswith("currency") or (inferred == "percent" and not red):
                    fmt = ""
                if fmt:
                    add.append(("format", f"{h}={fmt}"))
                    count["numfmt"] = count.get("numfmt", 0) + 1
            years = year or (fmt == "0")
            # data visuals
            if len(body) >= 3 and not years and not _covers(visual_vals, h) and st["nums"]:
                if toks & _ICON_WORDS:
                    add.append(("icons", h))
                    count["icons"] = count.get("icons", 0) + 1
                elif "score" in toks or "share" in toks or "% of" in h.lower():
                    add.append(("databar", h))
                    count["bars"] = count.get("bars", 0) + 1
            if ci > 0 and not years and toks & _ADDITIVE and not (toks & _RATIO_WORDS or "%" in h):
                totals.append(h)
            continue
        status = _status_colours(h, vals) if not st["nums"] or st["text"] else None
        if status and not _covers(visual_vals, h):
            rules, distinct = status
            for value, colour in rules:
                add.append(("highlight", f"{h} = {value} {colour}"))
            if rules:
                count["status"] = count.get("status", 0) + 1
                count.setdefault("colours", set()).update(c for _, c in rules)
            safe_vals = all(not re.search(r"[,\"'\]]", v) for v in distinct)
            if (2 <= len(distinct) <= 10 and safe_vals and len(",".join(distinct)) <= 250
                    and not _covers(vals_of("validate"), h)):
                add.append(("validate", f"{h} in [{', '.join(distinct)}] soft"))      # soft: a warning only, a new status may be typed
                count["lists"] = count.get("lists", 0) + 1
    # totals: they add a row under the table, so only where nothing points at the rows below it
    if (totals and safe and not explicit_total and len(body) >= 3 and not any(is_total_row(str(r[0])) for r in body)):
        add.append(("total", ", ".join(f"{h}=sum" for h in totals)))
        count["totals"] = count.get("totals", 0) + len(totals)
    # report layout: a banner moves the table down, so it needs a sheet nothing refers to by address
    has_addr = any(k in ("freeze", "merge", "color", "border", "align") and _ADDR_RX.search(v) for k, v in dirs)
    if len(spec["sheets"]) >= 2 and safe and not has_addr and not vals_of("banner"):
        add.append(("banner", s["name"]))
        count["banner"] = count.get("banner", 0) + 1
        lead = str(s.get("lead") or "").strip()
        if lead and not vals_of("subtitle") and not _CHATTER.match(lead) and lead.lower() != s["name"].lower() and len(lead) > 3:
            add.append(("subtitle", lead))
    s["directives"] = dirs + add
    s["numcols"] = numcols
    if s["auto"]:
        _auto_charts(s, spec)
        count["rowfmt"] = count.get("rowfmt", 0) + _row_formats(s, explicit_fmt)


def _n(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def enrich(spec: dict, cfg: dict = None) -> tuple:
    """Add the design a parsed workbook is missing. -> (spec, short report of what was added). The spec is changed in place; it
    is returned untouched (spec["auto"] False) when auto-design is off for the workbook, and a sheet marked off is skipped."""
    if cfg is None:
        from core.small_model import APP_CONFIG as cfg
    on = ((cfg or {}).get("sheet_engine") or {}).get("autodesign", True)
    spec["auto"] = bool(on) and str(spec.get("style", "")).lower() not in _OFF
    if not spec["auto"]:
        return spec, []
    unsafe, count = _unsafe_sheets(spec), {}
    spec["_chart_sources"] = {m.group(1).strip().lower() for s in spec["sheets"] if s["dashboard"] for k, v in s["directives"]
                              if k == "chart" for m in [re.search(r"(?i)\bsource\s*=\s*([^|]+)", v)] if m}
    for s in spec["sheets"]:
        off = any((k == "auto" and v.strip().lower() in _OFF) or (k == "style" and v.strip().lower() == "plain")
                  for k, v in s["directives"])
        s["auto"] = not off
        if s["header"] and not off:
            _design_sheet(s, spec, unsafe, count)
    labels = (("numfmt", "number formats on", "column"), ("datefmt", "date formats on", "column"), ("status", "status colours on", "column"),
              ("lists", "drop-down lists on", "column"), ("bars", "data bars on", "column"), ("icons", "trend icons on", "column"),
              ("totals", "totals on", "column"), ("rowfmt", "row formats on", "row"), ("band", "section bands:", ""), ("banner", "banners on", "sheet"))
    report = [f"{pre} {_n(count[k], noun) if noun else count[k]}" for k, pre, noun in labels if count.get(k)]
    if count.get("text"):
        report.append(f"{_n(count['text'], 'number')} stored as text")
    _extras(spec, count, report)
    return spec, report


# ---- charts ----------------------------------------------------------------------------------------------------------
_MON = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_PERIOD_RX = (
    re.compile(r"^(?:19|20)\d{2}$"),                                                             # 2024
    re.compile(r"^(?:FY|F\.Y\.?)\s*'?\d{2}(?:\d{2})?(?:\s*[-/]\s*\d{2}(?:\d{2})?)?$", re.I),     # FY2024, FY24, FY2024-25
    re.compile(r"^(?:19|20)\d{2}\s*[-/]\s*\d{2}(?:\d{2})?$"),                                    # 2024-25
    re.compile(r"^(?:Q[1-4]|H[12]|\d{1,2}M)\s*[-' ]?\s*(?:FY)?\s*'?\d{2}(?:\d{2})?(?:\s*[-/]\s*\d{2,4})?$", re.I),     # Q1 2025, H1-FY25
    re.compile(r"^(?:FY)?(?:19|20)\d{2}\s*[-' ]?\s*(?:Q[1-4]|H[12])$", re.I),                    # 2025 Q1
    _ISO_DATE,
    re.compile(r"^" + _MON + r"\.?(?:\s*[-'/ ]\s*(?:19|20)?\d{2})?$", re.I),                     # Jan, March 2025, Jan-25
)
_PERIOD_HEADERS = {"year", "fy", "fiscal year", "period", "quarter", "month", "date", "yr"}
_MAGNITUDE = 40          # columns whose typical sizes differ by more than this do not share one chart axis
_MONTH_ORDER = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def _is_period(t: str) -> bool:
    return any(rx.match(t.strip()) for rx in _PERIOD_RX)


def _period_key(t: str):
    """Sort key of a period label (so the newest can be found)."""
    t = t.strip()
    if _ISO_DATE.match(t):
        return tuple(int(x) for x in t.split("-"))
    low = t.lower()
    mon = re.match(r"^(" + _MON + r")", low)
    q = re.search(r"(?<![a-z])([qh])([1-4])(?!\d)", low)
    rest = low[:q.start()] + " " + low[q.end():] if q else low
    ym = re.search(r"((?:19|20)\d{2})", rest)
    year = int(ym.group(1)) if ym else None
    if year is None:
        y2 = re.search(r"(?<!\d)(\d{2})(?!\d)", rest)
        year = 2000 + int(y2.group(1)) if y2 else 0
    sub = 0
    if mon:
        sub = _MONTH_ORDER.index(mon.group(1)[:3]) + 1
    elif q:
        sub = int(q.group(2))
    return (year, sub)


def _fmt_of(s: dict, h: str) -> str:
    """The number format the sheet's directives (the model's or auto-design's) give column h, or ''."""
    for k, v in reversed(s["directives"]):
        if k == "format":
            for part in re.split(r"\s*,\s*(?=[^,=]+=)", v):
                if "=" in part:
                    n, f = part.split("=", 1)
                    if n.strip().lower() == h.lower():
                        return f.strip()
    return ""


def _header_row(s: dict) -> int:
    """Row of the table header exactly as the renderer places it: 1, 3 with a banner, 4 with banner and subtitle."""
    d = {}
    for k, v in s["directives"]:
        d[k] = v
    top = bool(str(d.get("banner", "")).strip()) + bool(str(d.get("subtitle", "")).strip())
    return top + 2 if top else 1


def _median(vals: list) -> float:
    v = sorted(abs(x) for x in vals)
    return (v[len(v) // 2] if v else 0) or 1


def _chart_cols(s: dict) -> list:
    """What each usable column of the sheet's table is, for chart and KPI choices."""
    header = [h or f"Column{i + 1}" for i, h in enumerate(s["header"][:MAX_COLS])]
    n = len(header)
    rows = [(list(r) + [""] * n)[:n] for r in s["rows"]]
    out = []
    for ci, h in enumerate(header):
        if not _usable_header(h, header):
            continue
        vals = [r[ci] for r in rows]
        st = _stats(vals)
        fmt = _fmt_of(s, h)
        nums = st["nums"]
        toks = set(_tokens(h))
        year = bool(nums) and (fmt == "0" or bool(re.fullmatch(r"(?i)year|fy|fiscal year|yr", h.strip()))
                               or all(re.fullmatch(r"(?:19|20)\d{2}", r) for r in st["raws"]))
        num = (h.lower() in s.get("numcols", set()) and len(nums) >= max(2, 0.6 * len(rows)) and st["text"] <= 0.25 * len(nums)
               and not st["formulas"] and not 0 < st["pct"] < len(nums))      # a mix of percents and plain numbers is no one scale
        pct = "%" in fmt or fmt.lower() in ("percent", "pct") or _infer(vals) == "percent" or "%" in h or bool(toks & _PCT_WORDS)
        out.append({"i": ci, "h": h, "vals": vals, "st": st, "num": num, "year": year, "pct": pct, "mag": _median(nums)})
    return out


def _groups(cols: list, size: int = 4) -> list:
    """Columns that can share one axis (typical sizes within a factor of _MAGNITUDE), at most `size` per group."""
    groups = []
    for c in cols:
        for g in groups:
            if len(g[1]) < size and 1 / _MAGNITUDE <= c["mag"] / g[0] <= _MAGNITUDE:
                g[1].append(c)
                break
        else:
            groups.append((c["mag"], [c]))
    return [g[1] for g in groups]


def _is_share(c: dict) -> bool:
    nums = c["st"]["nums"]
    if not 2 <= len(nums) <= 8 or any(v < 0 for v in nums):
        return False
    tot = sum(nums)
    return abs(tot - 1) <= 0.02 or (abs(tot - 100) <= 2 and c["pct"])


def _period_col(cols: list, n: int):
    return next((c for c in cols if (c["i"] == 0 or c["h"].lower() in _PERIOD_HEADERS) and n >= 3
                 and all(str(v).strip() and _is_period(str(v)) for v in c["vals"])), None)


def _similar_sizes(c: dict) -> bool:
    """False when a column's values span so many orders of magnitude that bars of it would be unreadable."""
    mags = [abs(v) for v in c["st"]["nums"] if v]
    return not mags or max(mags) / min(mags) <= 500


def _row_pct(label: str, unit: str, raws: list, nums: list) -> bool:
    """True when a metric row holds percentages / ratios (so it never shares an axis with money)."""
    return (unit.strip() == "%" or bool(set(_tokens(label)) & _PCT_WORDS) or "%" in label
            or all(_PCT.match(r) for r in raws) or (bool(nums) and all(abs(v) <= 1.5 for v in nums) and any(float(v) != int(v) for v in nums)))


def _wide(s: dict):
    """A table whose HEADER holds the periods (`Metric | Unit | FY2021 | ... | FY2025`): -> {c1, c2 (0-based first/last period
    column), cols 'first:last', metrics [{label, i (row index), pct, unit, mag, vals}]}, or None. The period columns are the longest
    run of period-like header cells; mostly empty/text columns at its edges are dropped; a metric row needs mostly numeric cells."""
    header = [h or f"Column{i + 1}" for i, h in enumerate(s["header"][:MAX_COLS])]
    n = len(header)
    rows = [(list(r) + [""] * n)[:n] for r in s["rows"]]
    lows = [h.lower() for h in header]
    if n < 4 or len(rows) < 2 or len(set(lows)) != n:
        return None
    flags = [ci >= 1 and _is_period(header[ci]) for ci in range(n)]
    best, run = (0, 0), None
    for ci in range(n + 1):
        if ci < n and flags[ci]:
            run = (run[0], ci) if run else (ci, ci)
            if run[1] - run[0] > best[1] - best[0] or best == (0, 0):
                best = run
        else:
            run = None
    c1, c2 = best
    if c1 == 0:
        return None
    body = [r for r in rows if not str(r[0]).startswith(SECTION_PREFIX)]
    if not body or sum(1 for r in body if _to_number(r[0]) is not None) > 0.3 * len(body):
        return None
    share = lambda ci: sum(1 for r in body if _to_number(r[ci]) is not None) / len(body)      # noqa: E731
    while c1 <= c2 and share(c1) < 0.5:
        c1 += 1
    while c2 >= c1 and share(c2) < 0.5:
        c2 -= 1
    if c2 - c1 + 1 < 3 or any(not flags[ci] for ci in range(c1, c2 + 1)):
        return None
    ucol = next((ci for ci in range(1, c1) if set(_tokens(header[ci])) & {"unit", "units", "uom"}), None)
    metrics, seen = [], set()
    for i, r in enumerate(rows):
        label = str(r[0]).strip()
        if not label or label.startswith(SECTION_PREFIX) or re.search(r"[,|=]", label) or label.lower() in seen:
            continue
        cells = [str(r[ci]).strip() for ci in range(c1, c2 + 1)]
        nums = [_to_number(c) for c in cells]
        got = [v for v in nums if v is not None]
        if len(got) < max(3, 0.7 * (c2 - c1 + 1)):
            continue
        unit = str(r[ucol]).strip() if ucol is not None else ""
        raws = [c for c, v in zip(cells, nums) if v is not None]
        seen.add(label.lower())
        metrics.append({"label": label, "i": i, "pct": _row_pct(label, unit, raws, got), "unit": unit.lower(), "mag": _median(got),
                        "vals": nums, "raws": cells})
    if not metrics:
        return None
    return {"c1": c1, "c2": c2, "cols": f"{header[c1]}:{header[c2]}", "metrics": metrics, "header": header, "ucol": ucol}


def _row_formats(s: dict, explicit: set) -> int:
    """Wide period table: every metric row gets its own number format (`rowformat: Label=fmt`), because a column holds a percent
    row and an amount row at once. Skipped when the model formatted any of the period columns itself. -> rows formatted."""
    w = _wide(s)
    if not w or any(w["header"][ci].lower() in explicit for ci in range(w["c1"], w["c2"] + 1)):
        return 0
    have = {v.rpartition("=")[0].strip().lower() for k, v in s["directives"] if k == "rowformat"}
    made = 0
    for m in w["metrics"]:
        if m["label"].lower() in have:
            continue
        if m["pct"]:
            fractional = all(abs(v) <= 1.5 for v in m["vals"] if v is not None)
            f = "0.0%" if fractional else '0.0"%"'
        else:
            dec = max(_stats([r])["dec"] for r in m["raws"] if _to_number(r) is not None)
            f = {0: "#,##0", 1: "#,##0.0"}.get(dec, "#,##0.00")
        s["directives"].append(("rowformat", f"{m['label']}={f}"))
        made += 1
    return made


def _row_groups(ms: list, size: int = 4) -> list:
    groups = []
    for m in ms:
        for g in groups:
            if len(g[1]) < size and g[1][0]["unit"] == m["unit"] and 1 / _MAGNITUDE <= m["mag"] / g[0] <= _MAGNITUDE:
                g[1].append(m)
                break
        else:
            groups.append((m["mag"], [m]))
    return [g[1] for g in groups]


def _plan_wide(s: dict, w: dict) -> list:
    name = s["name"].replace("|", "/")
    nper = w["c2"] - w["c1"] + 1
    absm = [m for m in w["metrics"] if not m["pct"]]
    pctm = [m for m in w["metrics"] if m["pct"]]
    plans = []
    for gi, g in enumerate((_row_groups(absm) + _row_groups(pctm))[:2]):
        pct = g[0]["pct"]
        fractional = all(abs(v) <= 1.5 for m in g for v in m["vals"] if v is not None)
        yfmt = ("0%" if fractional else '0"%"') if pct else "#,##0"
        labels = [m["label"] for m in g]
        title = f"{labels[0]} by period" if len(g) == 1 else name + (" (%)" if pct else " (2)" if gi else "")
        plans.append({"kind": "line" if nper > 6 else "column", "by": "row", "cols": w["cols"], "ys": labels, "title": title,
                      "yfmt": yfmt, "flags": [], "x": ""})
    return plans


def _plan_charts(s: dict) -> list:
    """[{kind, x, ys, title, flags}] the sheet's data calls for (at most 2): a period series, category/value, or a share."""
    cols = _chart_cols(s)
    rows = s["rows"]
    n = len(rows)
    if n < 2 or any(str(r[0]).startswith(SECTION_PREFIX) for r in rows if r):
        return []
    name = s["name"].replace("|", "/")
    usable = [c for c in cols if c["num"] and not c["year"]]
    px = _period_col(cols, n)
    plans = []
    if px:
        ys = [c for c in usable if c is not px]
        groups = _groups([c for c in ys if not c["pct"]]) + _groups([c for c in ys if c["pct"]])
        for gi, g in enumerate(groups[:2]):
            names = [c["h"] for c in g]
            title = f"{names[0]} by {px['h']}" if len(g) == 1 else name + (" (%)" if g[0]["pct"] else " (2)" if gi else "")
            plans.append({"kind": "line" if n > 6 else "column", "x": px["h"], "ys": names, "title": title, "flags": []})
        return plans
    wide = _wide(s)
    if wide:
        return _plan_wide(s, wide)
    if is_total_row(str(rows[-1][0])):
        return []
    lab = next((c for c in cols[:2] if not c["num"] and not c["year"] and c["st"]["text"] >= 0.8 * n
                and len({str(v).strip().lower() for v in c["vals"]}) == n), None)
    if lab is None:
        return []
    if any(set(_tokens(c["h"])) & {"unit", "units", "uom"} for c in cols):
        return []                                             # a column of mixed units (Metric | Value | Unit) is no chart
    nums = [c for c in usable if c is not lab and _similar_sizes(c)]
    shares = [c for c in nums if c["pct"] and _is_share(c)]
    pie = shares[0] if len(shares) == 1 else None
    rest = [c for c in nums if c is not pie]
    labels = [str(v) for v in lab["vals"]]
    flags = ["horizontal"] if max(map(len, labels)) > 16 or sum(map(len, labels)) / n > 11 else []
    cand = []
    absg = _groups([c for c in rest if not c["pct"]], 2)
    pctg = _groups([c for c in rest if c["pct"]], 2)
    if 3 <= n <= 15 and absg:
        cand.append(("bar", absg[0], flags))
    if pie is not None:
        cand.append(("doughnut", [pie], []))
    if 3 <= n <= 15 and pctg:
        cand.append(("bar", pctg[0], flags))
    for kind, g, fl in cand[:2]:
        names = [c["h"] for c in g]
        title = f"{names[0]} by {lab['h']}" if len(g) == 1 else f"{name} by {lab['h']}"
        plans.append({"kind": kind, "x": lab["h"], "ys": names, "title": title, "flags": fl})
    return plans


def _chart_line(p: dict, source: str = "") -> str:
    parts = [p["kind"], f"title={p['title'].replace('|', '/')}"]
    if source:
        parts.append(f"source={source}")
    if p.get("by") == "row":
        parts += ["by=row", f"cols={p['cols']}", f"rows={','.join(p['ys'])}", f"yfmt={p['yfmt']}"]
    else:
        parts += [f"x={p['x']}", f"y={','.join(p['ys'])}"]
    return " | ".join(parts + list(p["flags"]))


def _auto_charts(s: dict, spec: dict) -> None:
    """Add `chart:` directives for what the sheet's table is. Nothing when the model drew a chart itself (here or on a dashboard
    from this sheet), when more tables share the sheet (the charts' place is taken) or when the table has no clear shape."""
    if (s["dashboard"] or s["blocks"] or any(k == "chart" for k, _ in s["directives"])
            or s["name"].lower() in spec.get("_chart_sources", ())):
        return
    plans = _plan_charts(s)
    s["auto_charts"] = plans
    for p in plans:
        s["directives"].append(("chart", _chart_line(p)))


# ---- contents sheet and dashboard ------------------------------------------------------------------------------------
_SUMMARY_NAME = re.compile(r"(?i)executive|summary|kpi|overview|highlight|\bkey\b")
_VALUE_WORDS = {"value", "latest", "current", "amount", "actual", "figure", "result", "reading"}
_NOT_VALUE_WORDS = {"growth", "yoy", "change", "prior", "previous", "cagr", "share", "rank", "source", "sources", "note", "notes",
                    "evidence", "delta", "peak", "lowest", "highest", "target"}
_METRIC_WORDS = {"metric", "metrics", "kpi", "indicator", "measure", "item", "particulars", "figure"}
_YEARISH_LABEL = {"year", "founded", "established", "since", "fy", "date"}
_CONTENTS_NAMES = {"contents", "index", "toc", "cover", "table of contents"}
_LEGEND = {"green": "Good, done or verified", "amber": "Pending, in progress or at risk", "red": "Problem, blocked or not disclosed",
           "grey": "Unknown, estimated or not applicable"}
MAX_AUTO_KPIS = 8


def _sheet_ref(x: str) -> str:
    plain = re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", x) and not re.fullmatch(r"[A-Za-z]{1,3}\d+", x)
    return x if plain else "'" + x.replace("'", "''") + "'"


def _tile_format(raw: str) -> str:
    """The format of a KPI tile for one source value."""
    k = _infer([raw])
    if k:
        return k
    dec = _stats([raw])["dec"]
    return "int" if dec == 0 else "#,##0.0" if dec == 1 else "#,##0.00"


def _col_tile_format(s: dict, c: dict, raw: str) -> str:
    """The tile format of a column: the format its sheet gave it (negative-number section dropped), else read from the value."""
    f = _fmt_of(s, c["h"]).split(";")[0].strip()
    if not f:
        return _tile_format(raw)
    if f.lower().startswith(("currency", "percent")):
        return f
    return {"0.0%": "percent", "#,##0": "int", "#,##0.00": "number"}.get(f, f)


def _label(text: str) -> str:
    t = re.sub(r"\s+", " ", re.sub(r"[=|]", " ", str(text))).strip().lstrip(">").strip().rstrip(":")
    return t if len(t) <= 40 else t[:39].rstrip() + "…"


def _summary_kpis(s: dict) -> list:
    """Rows of a Metric | Value style sheet whose value is a number -> [(label, column letter, index in rows, format)]."""
    if not _SUMMARY_NAME.search(s["name"]) or len(s["header"]) < 2 or len(s["rows"]) < 3:
        return []
    ncols = min(len(s["header"]), MAX_COLS)
    rows = [(list(r) + [""] * ncols)[:ncols] for r in s["rows"]]
    numeric = []
    for ci in range(1, ncols):
        filled = sum(1 for r in rows if str(r[ci]).strip().lower() not in PLACEHOLDERS)
        if filled and sum(1 for r in rows if _to_number(r[ci]) is not None) >= 0.5 * filled:
            numeric.append(ci)
    toks = lambda ci: set(_tokens(s["header"][ci]))      # noqa: E731
    vi = next((ci for ci in numeric if toks(ci) & _VALUE_WORDS), None)
    if vi is None:
        vi = next((ci for ci in numeric if not toks(ci) & _NOT_VALUE_WORDS), None)     # not a growth / prior-period / rank column
    if vi is None:
        return []
    # the label: a column named like a metric, else the text column left of the value with the most distinct entries
    # (`Category | Metric | Value`: the category repeats, the metric does not)
    left = [ci for ci in range(vi) if sum(1 for r in rows if _to_number(r[ci]) is not None) <= 0.3 * len(rows)]
    if not left:
        return []
    lc = next((ci for ci in left if set(_tokens(s["header"][ci])) & _METRIC_WORDS),
              max(left, key=lambda ci: (len({str(r[ci]).strip().lower() for r in rows}), -ci)))
    unit = next((ci for ci in range(vi + 1, ncols) if set(_tokens(s["header"][ci])) & {"unit", "units", "uom"}), None)
    out = []
    for i, r in enumerate(rows):
        label, raw = str(r[lc]).strip(), str(r[vi]).strip()
        if not label or label.startswith(SECTION_PREFIX) or _to_number(raw) is None or set(_tokens(label)) & _YEARISH_LABEL:
            continue
        text = _label(label)
        u = str(r[unit]).strip() if unit is not None else ""
        fmt = _tile_format(raw)
        n = _to_number(raw)
        if u == "%" and "." in raw and not _PCT.match(raw) and 0 < abs(n) <= 1.5:
            fmt = "percent"                                   # 0.51 with the unit % is a fraction: show 51.0%
        elif u and u.lower() not in PLACEHOLDERS and len(text) + len(u) + 3 <= 40 and not _PCT.match(raw):
            text = f"{text} ({u})"
        out.append((text, col_letter(vi + 1), i, fmt))
    return out


def _period_kpis(s: dict) -> list:
    """The newest row of a period series: its first 1-3 numeric columns -> [(label, column letter, index in rows, format)]."""
    cols = _chart_cols(s)
    n = len(s["rows"])
    px = _period_col(cols, n)
    if not px or any(str(r[0]).startswith(SECTION_PREFIX) for r in s["rows"] if r):
        return []
    keys = [_period_key(str(v)) for v in px["vals"]]
    last = 0 if keys[0] > keys[-1] else n - 1                  # a table may list the newest period first
    out = []
    for c in [c for c in cols if c["num"] and not c["year"] and c is not px]:
        raw = str(c["vals"][last]).strip()
        if _to_number(raw) is None:
            continue
        out.append((_label(f"{c['h']} {str(px['vals'][last]).strip()}"), col_letter(c["i"] + 1), last, _col_tile_format(s, c, raw)))
        if len(out) == 3:
            break
    return out


def _wide_kpis(s: dict) -> list:
    """Wide period table: per metric row the newest period with a number -> [(label, column letter, row index, format)]."""
    w = _wide(s)
    if not w:
        return []
    header = w["header"]
    cols = list(range(w["c1"], w["c2"] + 1))
    keys = {ci: _period_key(header[ci]) for ci in cols}
    order = sorted(cols, key=lambda ci: (keys[ci], ci), reverse=True)                   # newest period first
    out = []
    for m in w["metrics"]:
        ci = next((c for c in order if m["vals"][c - w["c1"]] is not None), None)
        if ci is None:
            continue
        raw, val = m["raws"][ci - w["c1"]], m["vals"][ci - w["c1"]]
        if m["pct"]:
            fmt = "percent" if abs(val) <= 1.5 else '0.0"%"'
        else:
            fmt = _tile_format(raw)
        text = _label(f"{m['label']} {header[ci]}")
        out.append((text, col_letter(ci + 1), m["i"], fmt))
    return out


_PERIOD_TOKEN = re.compile(r"(?i)\b(?:fy\s*)?'?(?:19|20)?\d{2}(?:\d{2})?(?:[-/](?:19|20)?\d{2,4})?\b|\b(?:[qh][1-4]|\d{1,2}m)\b")


def _metric_words(label: str) -> frozenset:
    return frozenset(re.findall(r"[a-z]+", _PERIOD_TOKEN.sub(" ", label.lower())))


def _label_year(label: str) -> int:
    ys = [int(y) for y in re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", label)]
    return max(ys) if ys else 0


def _dedupe(cands: list) -> list:
    """Year twins and near-duplicate labels (`Net profit 2025`, `Net profit 2024`) collapse to the latest one, in first-seen order.
    Each candidate is (label, sheet, letter, row index, format)."""
    kept = []
    for c in cands:
        words = _metric_words(c[0])
        for j, k in enumerate(kept):
            kw = _metric_words(k[0])
            if words == kw or (words and kw and len(words & kw) / len(words | kw) >= 0.8):
                if _label_year(c[0]) > _label_year(k[0]):
                    kept[j] = c
                break
        else:
            kept.append(c)
    return kept


def _pick_kpis(summary: list, period: list, limit: int = MAX_AUTO_KPIS) -> list:
    """The tiles: de-duplicated summary figures when there are 4 or more distinct ones; else those plus the newest figures of the
    period/wide sheets, taken round-robin so one sheet does not fill the dashboard."""
    summary = _dedupe(summary)
    if len(summary) >= 4:
        return summary[:limit]
    by_sheet = {}
    for c in _dedupe(period):
        by_sheet.setdefault(id(c[1]), []).append(c)
    pool, queues = list(summary), list(by_sheet.values())
    while len(pool) < limit and any(queues):
        for q in queues:
            if q and len(pool) < limit:
                pool.append(q.pop(0))
    return _dedupe(pool)[:limit]


def _description(s: dict) -> str:
    """What a sheet holds, for the Contents sheet: its subtitle or lead-in line, else its first column names."""
    sub = [v for k, v in s["directives"] if k == "subtitle"]
    lead = str(s.get("lead") or "").strip()
    if sub and sub[-1].strip():
        d = sub[-1].strip()
    elif lead and not _CHATTER.match(lead) and lead.lower() != s["name"].lower() and len(lead) > 3:
        d = lead
    else:
        names = [h for h in s["header"][:5] if h]
        d = ", ".join(names) + (", ..." if len(s["header"]) > 5 else "") + (f" ({_n(len(s['rows']), 'row')})" if s["rows"] else "")
    d = d.lstrip("=").strip()
    return d if len(d) <= 110 else d[:109].rstrip() + "…"


def _hidden(s: dict) -> bool:
    return any(k == "hide" and "sheet" in [x.strip().lower() for x in v.split(",")] for k, v in s["directives"])


def _extras(spec: dict, count: dict, report: list) -> None:
    """Contents sheet and Dashboard, when the workbook is big enough to need them. Their cell addresses are worked out here, from
    the final table positions, so the Dashboard's tiles are plain live formulas."""
    import datetime
    sheets = spec["sheets"]
    data = [s for s in sheets if s["header"]]
    used: set = set()
    for s in sheets:                                          # the names the renderer will give them (build() does the same)
        s["xname"] = _safe_sheet_name(s["name"], used)
    taken = {s["name"].lower() for s in sheets} | {s["xname"].lower() for s in sheets}
    charted = sum(1 for s in data if s.get("auto_charts"))
    if charted:
        report.append(f"charts on {_n(charted, 'sheet')}")
    dash = None
    live = [s for s in data if s.get("auto") and not _hidden(s)]
    room = MAX_SHEETS - len(sheets)                           # the extra sheets count against the workbook's sheet limit
    if room >= 1 and len(data) >= 3 and not any(s["dashboard"] for s in sheets) and "dashboard" not in taken:
        summary = [(label, s, letter, i, fmt) for s in live for label, letter, i, fmt in _summary_kpis(s)]
        period = [(label, s, letter, i, fmt) for s in live for label, letter, i, fmt in _period_kpis(s) + _wide_kpis(s)]
        kp = _pick_kpis(summary, period)
        if len(kp) >= 3:
            dirs = [("title", spec["title"] or "Dashboard")]
            for label, s, letter, i, fmt in kp:
                dirs.append(("kpi", f"{label} = ={_sheet_ref(s['xname'])}!{letter}{_header_row(s) + 1 + i} | format={fmt}"))
            charts = [_chart_line(s["auto_charts"][0], s["xname"]) for s in live if s.get("auto_charts") and "|" not in s["xname"]][:4]
            dirs += [("chart", c) for c in charts]
            dirs.append(("note", "Tiles are live formulas: they update when the data on the other sheets changes."))
            dash = {"name": "Dashboard", "header": None, "rows": [], "directives": dirs, "dashboard": True, "blocks": [],
                    "auto": True, "xname": "Dashboard"}
            report.append(f"Dashboard with {_n(len(kp), 'KPI tile')}" + (f" and {_n(len(charts), 'chart')}" if charts else ""))
        else:
            report.append(f"no Dashboard: {_n(len(kp), 'KPI candidate')} found (3 needed)")
    contents = None
    if len(data) >= 4 and not (_CONTENTS_NAMES & taken) and room - (1 if dash else 0) >= 1:
        listed = [s for s in ([dash] if dash else []) + sheets if not _hidden(s)]
        rows = [[str(i), s["xname"], _description(s) if s["header"] else "Dashboard: key figures and charts"]
                for i, s in enumerate(listed, 1)]
        contents = {"name": "Contents", "header": ["#", "Sheet", "What it holds"], "rows": rows, "blocks": [], "dashboard": False,
                    "directives": [("banner", spec["title"] or "Contents"), ("subtitle", datetime.date.today().isoformat()),
                                   ("link", "Sheet"), ("widths", "A=5, B=30, C=80"), ("freeze", "none"), ("filter", "off")],
                    "auto": True, "numcols": set(), "nogrid": True, "xname": "Contents", "lead": ""}
        colours = [c for c in (_GREEN, _AMBER, _RED, _GREY) if c in count.get("colours", ())]
        if colours:
            contents["blocks"].append({"title": "Colour legend", "at": "", "header": ["Colour", "Meaning"], "legend": True,
                                       "rows": [[c.capitalize(), _LEGEND[c]] for c in colours], "directives": []})
        report.append("Contents sheet")
    spec["sheets"] = [x for x in (contents, dash) if x] + sheets
