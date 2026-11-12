"""Read-only SQL over the sheets of an attached workbook / CSV (stdlib sqlite3, in memory, no new dependency).

The model gets joins, pivots (conditional aggregates), window functions, medians / percentiles and month / quarter bucketing
for real analysis; it cannot change anything. The data is loaded into a private in-memory database; after loading the
connection is locked: `PRAGMA query_only`, and an authorizer that only lets SELECT statements read the loaded tables
(no ATTACH, PRAGMA, DDL, DML, load_extension). A progress handler stops queries that run longer than QUERY_TIMEOUT_S.
Output is capped (rows and characters) and card numbers are masked before it is returned.
"""
import datetime as _dt
import difflib
import json
import math
import re
import secrets
import sqlite3
import threading
import time
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Optional

from ..file_tools.profile import column_stats, plain_long_int, read_tables
from ..pan import is_card_header, mask_cell

QUERY_TIMEOUT_S = 8.0
DEFAULT_ROWS = 50
MAX_ROWS = 200
MAX_OUT_CHARS = 6000
CELL_WIDTH = 40
MAX_CACHED = 4

_SQLITE_RECURSIVE = 33
_ALLOWED_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, _SQLITE_RECURSIVE}
_DENIED_FUNCTIONS = {"load_extension", "readfile", "writefile", "edit", "fts3_tokenizer",
                     "randomblob", "zeroblob",       # one call can allocate gigabytes; nothing here needs them
                     "hex", "unicode", "char", "quote"}   # they turn text into codes one character at a time: a way to read a masked value
MAX_VALUE_BYTES = 1_000_000                          # longest text / blob a query may build (SQLITE_LIMIT_LENGTH)
_MAX_HINT_NAMES = 20                                 # table names listed in an error message
_DMY = re.compile(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})$")
_QUOTED_RX = re.compile(r'"([^"]+)"')
_ALIAS_RX = re.compile(r'\bAS\s+"([^"]+)"', re.I)
_ALIAS_PLAIN_RX = re.compile(r"\bAS\s+([A-Za-z_]\w*)", re.I)   # AS activities, AS total_proposal
_CTE_RX = re.compile(r'(?:\bWITH\b|,)\s*"?([A-Za-z_]\w*)"?\s+AS\s*\(', re.I)
_CMP_LIT_RX = re.compile(r'(?:<=|>=|<>|!=|=|<|>|\bLIKE\b|\bGLOB\b)\s*"[^"]*"', re.I)
_IN_LIST_RX = re.compile(r"\bIN\s*\([^)]*\)", re.I | re.S)
_PATHY_RX = re.compile(r"[$[\]{}\\]")                       # a path or an expression, not a column name


class SqlError(Exception):
    pass


# ---- aggregates ----------------------------------------------------------------------------------------------------
class _Median:
    def __init__(self):
        self.v = []

    def step(self, x):
        if isinstance(x, (int, float)):
            self.v.append(float(x))

    def finalize(self):
        if not self.v:
            return None
        s = sorted(self.v)
        n = len(s)
        return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


class _Stdev:
    def __init__(self):
        self.v = []

    def step(self, x):
        if isinstance(x, (int, float)):
            self.v.append(float(x))

    def finalize(self):
        n = len(self.v)
        if n < 2:
            return None
        m = sum(self.v) / n
        return math.sqrt(sum((x - m) ** 2 for x in self.v) / (n - 1))


class _Percentile:
    def __init__(self):
        self.v, self.p = [], 0.5

    def step(self, x, p):
        if isinstance(x, (int, float)):
            self.v.append(float(x))
        if isinstance(p, (int, float)):
            self.p = min(max(float(p), 0.0), 1.0)

    def finalize(self):
        if not self.v:
            return None
        s = sorted(self.v)
        k = (len(s) - 1) * self.p
        lo, hi = math.floor(k), math.ceil(k)
        return s[lo] + (s[hi] - s[lo]) * (k - lo)


class _Mode:
    def __init__(self):
        self.c = Counter()

    def step(self, x):
        if x is not None:
            self.c[x] += 1

    def finalize(self):
        return self.c.most_common(1)[0][0] if self.c else None


# ---- reading a number out of a cell --------------------------------------------------------------------------------
# One grammar for both ways of querying (the copy path in Python, the in-place views and kb_query in SQL), so the same cell is
# the same number everywhere. A blank, text or anything that is not clearly one number is NULL, never 0, and never a guess:
# "12-34", "5-", "--5", "1.5.2", "1,5" (a decimal comma), a date, "007" (an identifier) and a 13+ digit value (a card or account
# number, which must not become a float) are all NULL. Accepted: 1200  1,200  12,34,567 (lakh grouping)  1,200.50  .5  5.
# +5  -5  (300) = -300  12%  and a currency sign or word before or after it: ৳ $ € £ ₹ Tk BDT USD EUR GBP INR Taka /-.
_WS = " \t\n\r "
_CURRENCY = ("taka", "tk.", "tk", "bdt", "usd", "eur", "gbp", "inr", "৳", "$", "€", "£", "₹", "/-")
_COMMA_RX = r"(?:\d{1,3}(?:,\d{3}){1,6}|\d{1,2}(?:,\d\d){1,5},\d{3})"
_CELL_NUM_RX = re.compile(rf"^(?P<ip>{_COMMA_RX}|\d*)(?:\.(?P<fp>\d*))?$", re.A)


def _ascii_lower(s: str) -> str:
    return re.sub(r"[A-Z]+", lambda m: m.group(0).lower(), s)      # sqlite's lower() only knows ASCII too


def parse_cell_number(v) -> Optional[float]:
    """The number in a cell, or None. The Python twin of num_expr (the two are tested against each other)."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return None if f != f or (f.is_integer() and abs(f) >= 1e12) else f
    if not isinstance(v, str):
        return None
    t = v
    if re.fullmatch(r"[0-9,.+()%" + re.escape(_WS) + "-]*", t) is None:            # a currency sign or word in it: mark it, then peel it off
        t = _ascii_lower(t)
        for tok in _CURRENCY:
            t = t.replace(tok, "~")
        t = t.strip(_WS)
        if t.startswith("~"):
            t = t[1:].strip(_WS)
        if t.endswith("~"):
            t = t[:-1].strip(_WS)
    else:
        t = t.strip(_WS)
    if t.endswith("%"):
        t = t[:-1]
    neg = False
    if len(t) >= 2 and t[0] == "(" and t[-1] == ")":
        t, neg = t[1:-1], True
    elif t[:1] in ("+", "-"):
        neg, t = t[0] == "-", t[1:]
    m = _CELL_NUM_RX.match(t)
    if not m or not any(ch.isdigit() for ch in t):
        return None
    ip = m.group("ip")
    digits = ip.replace(",", "")
    if (len(ip) > 1 and ip[0] == "0" and ip[1] in "0123456789,") or len(digits) > 12:
        return None
    n = float(t.replace(",", "") if t.replace(",", "") not in (".",) else "0")
    return -n if neg else n


def _sq(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _comma_patterns() -> str:
    """The shapes a thousands-grouped integer part can have, as GLOB tests on column `ip` (see _COMMA_RX)."""
    d = "[0-9]"
    pats = []
    for k in range(1, 7):                                           # 1,200  12,345  123,456,789 ...
        for first in (1, 2, 3):
            pats.append(d * first + (",ddd".replace("d", d)) * k)
    for k in range(2, 7):                                           # 1,23,456  12,34,567 (lakh grouping)
        for first in (1, 2):
            pats.append(d * first + (",dd".replace("d", d)) * (k - 1) + ",ddd".replace("d", d))
    return " OR ".join(f"ip GLOB {_sq(p)}" for p in pats)


def num_expr(x: str) -> str:
    """SQL for the number in the text expression `x`: REAL, or NULL for a blank, text or a value that is not one clear number.
    Same grammar as parse_cell_number. `x` is a column or json_extract(...); it is read once per row (the rest works on a
    one-row sub-select, which sqlite does not merge back into the caller)."""
    # layer 1: peel a currency sign / word off, but only a cell that has one (a plain number skips the replace chain)
    chain = f"lower(v)"
    for tok in _CURRENCY:
        chain = f"replace({chain}, {_sq(tok)}, '~')"
    plain = f"v NOT GLOB '*[^0-9,.+()% {_WS}-]*'"
    q = f"SELECT {x} AS v LIMIT 1"
    q = f"SELECT v, CASE WHEN {plain} THEN NULL ELSE trim({chain}, {_sq(_WS)}) END AS m FROM ({q}) LIMIT 1"
    q = (f"SELECT v, CASE WHEN m IS NULL THEN NULL WHEN m GLOB '~*' THEN trim(substr(m, 2), {_sq(_WS)}) ELSE m END AS m "
         f"FROM ({q}) LIMIT 1")
    q = (f"SELECT CASE WHEN m IS NULL THEN trim(v, {_sq(_WS)}) WHEN m GLOB '*~' THEN trim(substr(m, 1, length(m) - 1), {_sq(_WS)}) "
         f"ELSE m END AS c FROM ({q}) LIMIT 1")
    # layer 2: the percent sign, then accounting brackets or one sign
    q = f"SELECT CASE WHEN c GLOB '*%' THEN substr(c, 1, length(c) - 1) ELSE c END AS p FROM ({q}) LIMIT 1"
    q = (f"SELECT p GLOB '(*)' OR p GLOB '-*' AS neg, CASE WHEN p GLOB '(*)' AND length(p) > 2 THEN substr(p, 2, length(p) - 2) "
         f"WHEN p GLOB '[+-]*' THEN substr(p, 2) ELSE p END AS b FROM ({q}) LIMIT 1")
    q = f"SELECT neg, b, substr(b, 1, instr(b || '.', '.') - 1) AS ip FROM ({q}) LIMIT 1"
    # layer 3: only digits, commas and at most one point; commas in the grouping shapes; no leading zero; under 13 digits
    ok = (f"b <> '' AND b NOT GLOB '*[^0-9,.]*' AND b GLOB '*[0-9]*' AND length(b) - length(replace(b, '.', '')) <= 1 "
          f"AND b NOT GLOB '*.*,*' AND (instr(ip, ',') = 0 OR {_comma_patterns()}) AND ip NOT GLOB '0[0-9,]*' "
          f"AND length(replace(ip, ',', '')) <= 12")
    slow = (f"(SELECT CASE WHEN {ok} THEN CASE WHEN neg THEN -1.0 ELSE 1.0 END * CAST(replace(b, ',', '') AS REAL) END "
            f"FROM ({q}) WHERE 1)")
    # the common clean cells (1200, 1200.50, 12,345, 1,234,567) are decided by two glob tests, without the sub-selects
    d = "[0-9]"
    grouped = " OR ".join(f"{x} GLOB {_sq(first + (',' + d * 3) * k)}" for k in (1, 2) for first in (d, d * 2, d * 3))
    return (f"CASE WHEN {x} GLOB '[1-9]*' AND {x} NOT GLOB '*[^0-9.]*' AND {x} NOT GLOB '*.*.*' AND length({x}) <= 12 "
            f"THEN CAST({x} AS REAL) WHEN {x} GLOB '[1-9]*' AND ({grouped}) THEN CAST(replace({x}, ',', '') AS REAL) ELSE {slow} END")


def _is_long_or_zero_led(v) -> bool:
    """A cell that is an identifier, not an amount: a leading zero before a digit ("007") or 13+ digits (card, account)."""
    if isinstance(v, bool) or v is None:
        return False
    if isinstance(v, (int, float)):
        return float(v).is_integer() and abs(float(v)) >= 1e12
    s = str(v).strip()
    m = re.match(r"[^\d]*([\d,]*)", s)
    ip = m.group(1) if m else ""
    return bool(re.match(r"^0[\d,]", ip)) or len(ip.replace(",", "")) >= 13


def column_kind(values: list, header: str = "") -> str:
    """column_stats' kind, except that a column is only numbers when no cell in it is an identifier (leading zero, 13+ digits):
    "007" or a card number must not lose its zeros or its last digits in a float."""
    kind = column_stats(list(values), header)["kind"]
    if kind == "number" and any(_is_long_or_zero_led(v) for v in values):
        return "text"
    return kind


# ---- loading -------------------------------------------------------------------------------------------------------
def _ident(name: str, used: set) -> str:
    base = re.sub(r"[^A-Za-z0-9_]+", "_", str(name)).strip("_") or "t"
    if base[0].isdigit():
        base = "t_" + base
    out, n = base, 2
    while out.lower() in used:
        out, n = f"{base}_{n}", n + 1
    used.add(out.lower())
    return out


def _iso(v):
    if isinstance(v, _dt.datetime):
        return v.date().isoformat() if v.hour == v.minute == v.second == 0 else v.isoformat(sep=" ")[:19]
    if isinstance(v, _dt.date):
        return v.isoformat()
    if isinstance(v, str):
        s = v.strip()
        m = _DMY.match(s)
        if m:
            d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            y += 2000 if y < 100 else 0
            try:                                  # day first (Bangladesh / most of the world)
                return _dt.date(y, mo, d).isoformat()
            except ValueError:
                return s
        return s[:19].replace("T", " ") if re.match(r"^\d{4}-\d{2}-\d{2}", s) else s
    return v


def _convert(kind: str, v, card_column: bool = False):
    """One cell for the in-memory table: a number (NULL when it is not clearly one), an ISO date, or text. Text never keeps a card
    number: it is masked here, so a query cannot read the digits however it takes the value apart."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if kind == "number":
        return parse_cell_number(v)
    out = _iso(v) if kind == "date" else v
    if isinstance(out, bool):
        return str(out)
    if isinstance(out, (int, float)):
        if not (float(out).is_integer() and abs(float(out)) >= 1e12):
            return out
        out = str(int(out))                           # a 13+ digit whole number is an identifier: text, and masked if it is a card
    return mask_cell(str(out), card_column)


class Workbook:
    """A loaded file: the sqlite connection plus what the model needs to know to write queries."""

    def __init__(self, conn, tables: list, notes: list):
        self.conn, self.tables, self.notes = conn, tables, notes     # tables: [{name, sheet, file, cols, rows, ...}]
        self.lock = threading.Lock()


def _sql_mask(text):
    """kb_mask(cell): the cell with card numbers masked. It can only hide, so the authorizer lets a query call it."""
    try:
        return mask_cell(text)
    except Exception:                             # never hand a value back that was not checked
        return "[masked]"


def _sql_mask_card(text):
    try:
        return mask_cell(text, card_column=True)
    except Exception:
        return "[masked]"


def masked_text(x: str, card_column: bool = False) -> str:
    """SQL for the text of cell expression `x` with card numbers masked. A card number is at least 13 characters, so only
    longer cells that have a digit (or any non-ASCII character: Bengali and other digits count too) go through the Python function,
    so ordinary text costs nothing."""
    fn = "kb_mask_card" if card_column else "kb_mask"
    return f"CASE WHEN length({x}) >= 13 AND ({x} GLOB '*[0-9]*' OR {x} GLOB '*[^ -~]*') THEN {fn}({x}) ELSE {x} END"


def _register(conn) -> sqlite3.Connection:
    conn.create_aggregate("MEDIAN", 1, _Median)
    conn.create_aggregate("STDEV", 1, _Stdev)
    conn.create_aggregate("PERCENTILE", 2, _Percentile)
    conn.create_aggregate("MODE", 1, _Mode)
    conn.create_function("kb_mask", 1, _sql_mask, deterministic=True)
    conn.create_function("kb_mask_card", 1, _sql_mask_card, deterministic=True)
    return conn


def _connect() -> sqlite3.Connection:
    return _register(sqlite3.connect(":memory:", check_same_thread=False))


def _add_table(conn, tname: str, headers: list, rows: list) -> tuple:
    """Create `tname` and fill it; each column becomes REAL when it is mostly numbers, else TEXT (dates as ISO text).
    -> ([(column, type)], [date column names])"""
    cols, kinds, seen, dates = [], [], set(), []
    data = list(zip(*[(r + [None] * len(headers))[: len(headers)] for r in rows])) if rows else [[] for _ in headers]
    for h, values in zip(headers, data):
        kind = column_kind(list(values), h)
        kinds.append((kind, is_card_header(h)))
        h = str(h).replace('"', "'")              # a double quote in a header would end the quoted column name
        cname = h if h.lower() not in seen else f"{h}_{len(seen)}"
        seen.add(cname.lower())
        cols.append((cname, "REAL" if kind == "number" else "TEXT"))
        if kind == "date":
            dates.append(cname)
    conn.execute(f'CREATE TABLE "{tname}" (' + ", ".join(f'"{c}" {ty}' for c, ty in cols) + ")")
    width = len(kinds)
    conn.executemany(f'INSERT INTO "{tname}" VALUES (' + ",".join("?" * width) + ")",
                     (tuple(_convert(k, v, card) for (k, card), v in zip(kinds, (r + [None] * width)[:width])) for r in rows))
    return cols, dates


def _lock(conn) -> None:
    conn.commit()
    try:
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_VALUE_BYTES)
    except (AttributeError, sqlite3.Error):       # Python < 3.11 has no setlimit
        pass
    conn.execute("PRAGMA query_only = ON")
    conn.set_authorizer(_authorizer)


def _build(paths: list) -> Workbook:
    conn = _connect()
    used, tables, notes = set(), [], []
    multi = len(paths) > 1
    for path in paths:
        stem = _ident(Path(path).stem, set())
        fname = Path(path).name
        for t in read_tables(path):
            tname = _ident(f"{stem}_{t.name}" if multi else t.name, used)
            cols, dates = _add_table(conn, tname, t.headers, t.rows)
            tables.append({"name": tname, "sheet": t.name, "file": fname, "cols": cols, "dates": dates, "rows": t.total_rows,
                           "truncated": t.truncated, "formulas": dict(t.formulas), "preamble": list(t.preamble),
                           "handle": f"T{len(tables) + 1}",
                           "aliases": [tname, t.name, fname, stem, f"{stem}_{t.name}", f"{fname}_{t.name}"]})
            if t.truncated:
                notes.append(f"{tname}: only the first {len(t.rows):,} of {t.total_rows:,} rows are loaded")
    _lock(conn)
    return Workbook(conn, tables, notes)


def from_tables(items: list) -> Workbook:
    """A Workbook from in-memory tables: [(name, source label, headers, rows)] or [(name, label, headers, rows, sheet)]
    or [(name, label, headers, rows, sheet, handle)] (the knowledge base's sheets). Not cached: the rows behind it can
    change and are access-controlled by the caller.

    `name` becomes the SQL table name (made a safe identifier), `label` the document it came from, `sheet` the tab inside
    it - the model needs the sheet name to tell two tabs of the same document apart - and `handle` a short name for it
    (the knowledge base uses the row id of the sheet, so a handle never points at another sheet after a re-index).
    """
    conn = _connect()
    used, tables = set(), []
    for item in items:
        name, label, headers, rows = item[0], item[1], item[2], item[3]
        sheet = item[4] if len(item) > 4 else name
        handle = item[5] if len(item) > 5 else f"T{len(tables) + 1}"
        tname = _ident(name, used)
        cols, dates = _add_table(conn, tname, headers, rows)
        aliases = [tname, name, sheet, label]
        if label and sheet and label != sheet:
            aliases.append(f"{label}_{sheet}")
        tables.append({"name": tname, "sheet": sheet, "file": label, "cols": cols, "dates": dates, "rows": len(rows), "truncated": False,
                       "formulas": {}, "preamble": [], "handle": handle, "aliases": aliases})
    _lock(conn)
    return Workbook(conn, tables, [])


# ---- the knowledge base's stored rows, queried in place -------------------------------------------------------------
# kb_sql used to copy every row of every table into a fresh in-memory database before running one query (seconds and
# hundreds of MB for a big file). Here the rows stay where they are: a second, read-only connection to the same file gets one
# TEMP VIEW per readable table, and the model's SQL runs against those views. The connection is locked so that the views are
# the ONLY thing it can read (the real tables hold every source's rows, keyed by table id).
KB_SAMPLE_ROWS = 500           # rows looked at to decide which columns are numbers: the first ones, and as many spread over the table
KB_SPARSE_MAX = 6              # at most this many such columns per table are re-read (each can cost one pass over a big table)
KB_SPARSE_MIN = 50             # a column with fewer non-empty cells than this in that sample is looked at again over the whole table


def open_readonly(path: str) -> sqlite3.Connection:
    """A second, read-only connection to the database file (never the app's own connection)."""
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    return _register(sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=10))


def _kb_cell(i: int) -> str:
    return f"json_extract(cells, '$[{int(i)}]')"


def _kb_num(i: int) -> str:
    """SQL for one numeric column: REAL, or NULL for blanks, text and anything that is not one clear number (never 0)."""
    return num_expr(_kb_cell(i))


def _kb_date(i: int, card_column: bool = False) -> str:
    """SQL for a date column as ISO text, like _iso for the copy path: day-first d/m/y (or d-m-y) becomes YYYY-MM-DD, an ISO
    value keeps its first 19 characters, anything else stays as stored (masked: a column of dates can hold other text too).
    A day the month does not have (31/02/2026) is not a date: it stays as written instead of rolling over into March."""
    c = _kb_cell(i)
    v = f"trim({c})"
    sl = f"replace({v}, '-', '/')"
    rest = f"substr({sl}, instr({sl}, '/') + 1)"
    d, mo, y = (f"substr({sl}, 1, instr({sl}, '/') - 1)", f"substr({rest}, 1, instr({rest}, '/') - 1)",
                f"substr({rest}, instr({rest}, '/') + 1)")
    yy = f"CASE WHEN length({y}) = 2 THEN '20' || {y} ELSE {y} END"
    yn, mn, dn = f"CAST({yy} AS INTEGER)", f"CAST({mo} AS INTEGER)", f"CAST({d} AS INTEGER)"
    leap = f"({yn} % 4 = 0 AND ({yn} % 100 <> 0 OR {yn} % 400 = 0))"
    last = f"CASE WHEN {mn} = 2 THEN CASE WHEN {leap} THEN 29 ELSE 28 END WHEN {mn} IN (4, 6, 9, 11) THEN 30 ELSE 31 END"
    ok = (f"({v} NOT GLOB '[0-9][0-9][0-9][0-9]-*' AND {sl} GLOB '*/*/*' AND {sl} NOT GLOB '*[^0-9/]*' "
          f"AND length({d}) BETWEEN 1 AND 2 AND length({mo}) BETWEEN 1 AND 2 AND length({y}) IN (2, 4) "
          f"AND {mn} BETWEEN 1 AND 12 AND {dn} BETWEEN 1 AND {last})")
    return (f"CASE WHEN {v} = '' THEN NULL WHEN {ok} THEN printf('%s-%02d-%02d', {yy}, {mn}, {dn}) "
            f"WHEN {v} GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*' THEN replace(substr({v}, 1, 19), 'T', ' ') "
            f"ELSE {masked_text(c, card_column)} END")


def _kb_sample(conn, tid: int, nrows: int, ncols: int) -> tuple:
    """Rows to judge the column types by: the first KB_SAMPLE_ROWS and the same number spread over the whole table (every k-th
    stored row), so a column that is blank or text at the top and numbers further down is still seen as numbers. A column that
    stays nearly empty in that sample is read again from its own first non-empty cells."""
    k = max(1, int(nrows or 0) // KB_SAMPLE_ROWS)
    rows = [json.loads(c) for (c,) in conn.execute(
        "SELECT cells FROM main.kb_rows WHERE table_id = ? ORDER BY rownum LIMIT ?", (tid, KB_SAMPLE_ROWS))]
    if k > 1:
        rows += [json.loads(c) for (c,) in conn.execute(
            "SELECT cells FROM main.kb_rows WHERE table_id = ? AND rowid % ? = 0 LIMIT ?", (tid, k, KB_SAMPLE_ROWS))]
    sparse = []
    for i in range(ncols):
        if int(nrows or 0) > len(rows) and sum(1 for r in rows if i < len(r) and str(r[i] or "").strip()) < KB_SPARSE_MIN:
            sparse.append(i)
    return rows, sparse[:KB_SPARSE_MAX]


def from_kb_views(conn, metas: list) -> Workbook:
    """A Workbook over TEMP VIEWs on `conn` (see open_readonly), one per entry of `metas` (kb_tables.list_tables rows: id,
    title, sheet, columns, nrows). Access was decided by the caller: a table that is not in `metas` has no view.

    Every text cell reaches the model's SQL already masked (kb_mask: a card number is hidden whatever the query does with the
    value) and every number cell is parsed by the same grammar as the copy path."""
    reserved = {"sqlite_master", "sqlite_temp_master", "sqlite_schema", "sqlite_sequence"}
    reserved |= {str(r[0]).lower() for r in conn.execute("SELECT name FROM main.sqlite_master")}   # no view may shadow a real table
    used, tables, names, inners = set(reserved), [], set(), set()
    for m in metas:
        tid = int(m["id"])                                  # an integer from our own database, never text from the model
        base = m.get("name") or (f"{m['title']}_{m['sheet']}" if m["sheet"] else m["title"])
        tname = _ident("t_" + base if base.lower().startswith("sqlite_") else base, used)   # sqlite_* names are reserved
        headers = list(m["columns"] or [])
        nrows = int(m["nrows"])
        sample, sparse = _kb_sample(conn, tid, nrows, len(headers) or 1)
        if not headers:
            headers = [f"col{i + 1}" for i in range(max((len(r) for r in sample), default=1))]
            sparse = []
        sel, cols, seen, dates = [], [], set(), []
        for i, h in enumerate(headers):
            vals = [r[i] if i < len(r) else None for r in sample]
            if i in sparse:                                 # nearly empty in the sample: its own first non-empty cells decide
                vals += [c for (c,) in conn.execute(
                    f"SELECT {_kb_cell(i)} FROM main.kb_rows WHERE table_id = ? AND trim({_kb_cell(i)}) <> '' "
                    "ORDER BY rownum LIMIT ?", (tid, KB_SAMPLE_ROWS))]
            kind = column_kind(vals, str(h))
            card = is_card_header(h)
            h = str(h).replace('"', "'").strip() or f"col{i + 1}"
            cname = h if h.lower() not in seen else f"{h}_{len(seen)}"
            seen.add(cname.lower())
            cols.append((cname, "REAL" if kind == "number" else "TEXT"))
            if kind == "date":
                dates.append(cname)
            c = _kb_cell(i)
            expr = (_kb_num(i) if kind == "number" else _kb_date(i, card) if kind == "date"
                    else f"CASE WHEN trim({c}) = '' THEN NULL ELSE {masked_text(c, card)} END")
            sel.append(f'{expr} AS "{cname}"')
        # no row-number column: a second table with the same extra column would be joined on it by NATURAL JOIN, and SELECT *
        # would show a column the copy path does not have. The stored order is the sheet's order.
        # two layers: a hidden inner view (random name, never shown to the model) is the only thing allowed to read the stored
        # rows; the public view just selects from it. The authorizer cannot tell a view from a CTE of the same name by name
        # alone, so the public name must not be what opens the base table.
        inner = f"__kb_{secrets.token_hex(12)}"
        conn.execute(f'CREATE TEMP VIEW "{inner}" AS SELECT ' + ", ".join(sel) + f" FROM main.kb_rows WHERE table_id = {tid}")
        conn.execute(f'CREATE TEMP VIEW "{tname}" AS SELECT * FROM "{inner}"')
        inners.add(inner)
        names.add(tname.lower())
        aliases = [tname, base, m["sheet"] or base, m["title"]]
        if m["sheet"] and m["title"] != m["sheet"]:
            aliases.append(f"{m['title']}_{m['sheet']}")
        tables.append({"name": tname, "sheet": m["sheet"] or base, "file": m["title"], "cols": cols, "dates": dates,
                       "rows": nrows, "truncated": False, "formulas": {}, "preamble": [],
                       "handle": m.get("handle") or f"T{len(tables) + 1}", "aliases": aliases})
    # the caller listed the tables a moment ago: if one was replaced or removed since (a re-index), the views would describe
    # rows that are no longer those. Say so, and the caller copies the current rows instead.
    for m in metas:
        row = conn.execute("SELECT nrows FROM main.kb_tables WHERE id = ?", (int(m["id"]),)).fetchone()
        if row is None or int(row[0]) != int(m["nrows"]):
            raise RuntimeError(f"table {m['id']} changed while the views were built")
    _lock_kb(conn, names, inners)
    return Workbook(conn, tables, [])


def _lock_kb(conn, view_names: set, inner_names: set) -> None:
    """Same limits as _lock, but the stored rows can be read only by our hidden inner views, and those only through our public
    views. A public view may be read from anywhere (a CTE over it is fine); a CTE named like a view opens nothing."""
    try:
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_VALUE_BYTES)
    except (AttributeError, sqlite3.Error):
        pass
    conn.execute("PRAGMA query_only = ON")
    real = {str(r[0]).lower() for r in conn.execute("SELECT name FROM main.sqlite_master")}

    def auth(action, arg1, arg2, db, source):
        if action not in _ALLOWED_ACTIONS:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION:
            return sqlite3.SQLITE_DENY if str(arg2 or arg1 or "").lower() in _DENIED_FUNCTIONS else sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            name = str(arg1 or "").lower()
            reader = str(source or "").lower()
            if name.startswith("sqlite_"):
                return sqlite3.SQLITE_DENY
            if str(db).lower() == "main":             # a real table (kb_rows, kb_tables, chunks ...): only an inner view reads it
                return sqlite3.SQLITE_OK if reader in inner_names else sqlite3.SQLITE_DENY
            if name in inner_names:                   # an inner view: only a public view of ours reads it
                return sqlite3.SQLITE_OK if reader in view_names else sqlite3.SQLITE_DENY
            if name in view_names:                    # a public view, read from anywhere
                return sqlite3.SQLITE_OK
            # no database at all: a CTE reading itself (WITH RECURSIVE r ... FROM r). The same "no database" also comes with a read
            # of a real table that needs no column (SELECT COUNT(*) FROM kb_rows), so only a name that is no real table passes.
            return sqlite3.SQLITE_OK if db is None and name not in real else sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    conn.set_authorizer(auth)


def _authorizer(action, arg1, arg2, db, source):
    if action not in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and str(arg2 or arg1 or "").lower() in _DENIED_FUNCTIONS:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_READ and str(arg1 or "").lower().startswith("sqlite_"):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


_CACHE: "OrderedDict" = OrderedDict()
_CACHE_LOCK = threading.Lock()


def load(paths) -> Workbook:
    paths = [Path(p) for p in ([paths] if isinstance(paths, (str, Path)) else paths)]
    key = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in paths)
    with _CACHE_LOCK:
        wb = _CACHE.get(key)
        if wb is not None:
            _CACHE.move_to_end(key)
            return wb
    wb = _build(paths)
    with _CACHE_LOCK:
        _CACHE[key] = wb
        while len(_CACHE) > MAX_CACHED:
            _CACHE.popitem(last=False)
    return wb


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


# ---- describing and querying ---------------------------------------------------------------------------------------
def describe(wb: Workbook) -> str:
    lines = ["Tables (use these exact names, or the short handle; double-quote columns with spaces; dates are ISO text "
             "YYYY-MM-DD, numbers are REAL):"]
    for t in wb.tables:
        lines.append(f'- {t["name"]}  =  {t.get("handle") or "T?"}  ({t["rows"]:,} rows, sheet "{t["sheet"]}" of {t["file"]})')
        lines.append("    " + ", ".join(f'"{c}" {ty}' for c, ty in t["cols"][:60]) + (" ..." if len(t["cols"]) > 60 else ""))
        if t.get("dates"):
            lines.append("    date columns: " + ", ".join(f'"{c}"' for c in t["dates"][:20]) + " (day-first d/m/y is assumed for 05/03/2026 = 5 March)")
        if t["formulas"]:
            lines.append("    formula columns: " + ", ".join(f"{h} ({f})" for h, f in list(t["formulas"].items())[:6]))
        if t["preamble"]:
            lines.append("    title rows above the header (ignored): " + " | ".join(t["preamble"][:2]))
    lines += wb.notes
    lines.append("Extra functions: MEDIAN(x), STDEV(x), PERCENTILE(x, 0.9), MODE(x); strftime('%Y-%m', date_col) for month, "
                 "'%Y' year; window functions and CTEs work. SELECT only. Card numbers in the data are masked ([card ****1234]); "
                 "hex(), unicode(), char() and quote() are not available.")
    return "\n".join(lines)


def _fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        long_id = plain_long_int(v)
        if long_id is not None:
            return long_id
    if isinstance(v, float):
        return f"{v:,.2f}" if abs(v) < 1e15 and not v.is_integer() else f"{v:,.0f}"
    s = re.sub(r"\s+", " ", str(v)).strip()
    return s if len(s) <= CELL_WIDTH else s[: CELL_WIDTH - 1] + "…"


# ---- explaining a failed query --------------------------------------------------------------------------------------
def _norm(s: str) -> str:
    """A name reduced to its letters and digits: 'BFSI activity nov 20,2026' and 'bfsi_activity_nov_202026' are the same name."""
    return re.sub(r"[^a-z0-9]+", "", str(s).lower())


def _letters(s: str) -> str:
    return re.sub(r"[^a-z]+", "", str(s).lower())


def _digits(s: str) -> str:
    return re.sub(r"\D", "", str(s))


def _is_subsequence(short: str, long: str) -> bool:
    """True when every character of `short` appears in `long` in order ('26' in '2026')."""
    it = iter(long)
    return all(c in it for c in short)


def _resolve_ref(wb: Workbook, ident: str) -> tuple:
    """The loaded table a FROM/JOIN reference names, and how it was found: (name, "exact" | "guess") or ("", "").

    A model writes the name it knows - the file name, with the space still a space, a comma in it, or a digit typed
    wrong - and the exact identifier it must use is a mangled version of it. Rather than fail, the reference is matched
    against the names a table answers to (its handle, its identifier, its document title, its file name, its tab).

    A match counts only when exactly one table fits, so an ambiguous guess is still an error instead of a wrong answer:
    the numbers of the wrong document are worse than an error message. A reference that only STARTS with a table's name is
    reported as "near", which the caller turns into an error that lists the candidates.
    """
    ident = str(ident or "").strip().strip("\"`[]")
    if not ident:
        return "", ""
    low = ident.lower()
    for t in wb.tables:                              # the handle: short, exact, and a stale one simply does not exist
        if t.get("handle") and t["handle"].lower() == low:
            return t["name"], "exact"
    exact = [t for t in wb.tables if t["name"].lower() == low]
    if len(exact) == 1:
        return exact[0]["name"], "exact"
    nr = _norm(ident)
    if len(nr) < 4:
        return "", ""
    best = {}
    for t in wb.tables:
        top = 0
        for a in t.get("aliases", ()):
            na = _norm(a)
            if not na:
                continue
            if nr == na:
                top = max(top, 4)                 # the very name
            elif len(nr) >= 6 and na.startswith(nr):
                top = max(top, 3)                 # the reference is the shorter form ("sales.xlsx" for "sales.xlsx_Sales")
            elif len(nr) >= 6 and nr.startswith(na):
                top = max(top, 2)                 # the reference carries more ("BFSI..._xlsx" for "BFSI...")
            elif (_letters(a) == _letters(ident) and _digits(a) and _digits(ident)
                  and (_digits(ident) == _digits(a)
                       or _is_subsequence(_digits(ident), _digits(a))
                       or _is_subsequence(_digits(a), _digits(ident)))):
                top = max(top, 1)                 # the same letters, a digit typed wrong ("2_6" for "2026")
        if top:
            best[t["name"]] = top
    if best:
        top = max(best.values())
        hits = [n for n, s in best.items() if s == top]
        if len(hits) == 1:
            # "near": the reference is a table's name PLUS more ("Beta_Typo" for "Beta_B"). That is not a spelling of the name,
            # it is another name, and reading it as the table silently drops or swaps a table of a join.
            return hits[0], ("exact" if top >= 4 else "near" if top == 2 else "guess")
    # No fuzzy matching beyond that: "…2027…" must not be read as "…2026…". A name that is still wrong is answered
    # with the tables that exist and their handles, and the handle cannot be mistyped into another table.
    return "", ""


_ITEM_RX = re.compile(r'(?s)^\s*(?P<name>"(?:[^"]|"")+"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][\w$]*(?:\.[\w$]+)*)'
                      r'\s*(?:AS\s+)?(?P<alias>[A-Za-z_]\w*)?\s*$', re.I)
_FROM_CLAUSE_RX = re.compile(
    r'(?is)\b(?P<kw>from|join)(?P<sep>\s+)(?P<list>.*?\S)(?P<tail>\s*)'
    r'(?=\bwhere\b|\bgroup\b|\border\b|\bhaving\b|\blimit\b|\bjoin\b|\bon\b'
    r'|\bunion\b|\bintersect\b|\bexcept\b|\boffset\b|$)')


def _split_items(text: str) -> list:
    """The comma separated references of a FROM clause, quotes and parentheses respected."""
    items, buf, depth, quote = [], "", 0, ""
    for ch in text:
        if quote:
            buf += ch
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'`":
            quote = ch
            buf += ch
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            items.append(buf)
            buf = ""
            continue
        buf += ch
    if buf.strip():
        items.append(buf)
    return items


def _handle_of(wb: Workbook, name: str) -> str:
    return next((str(t.get("handle")) for t in wb.tables if t["name"] == name), "?")


def _rewrite_tables(wb: Workbook, sql: str) -> tuple:
    """Point the FROM/JOIN references at the tables that are really loaded.

    Only a reference that names exactly one loaded table is rewritten, so this can never turn a query into a read of a
    different document. Everything else is left as written and sqlite reports it. Returns the new statement and the
    tables that were found by a guess rather than by their exact name.
    """
    guessed = []

    def clause(m):
        kw, sep, lst, tail = m.group("kw"), m.group("sep"), m.group("list"), m.group("tail")
        parts = _split_items(lst)
        if not parts or any("(" in p or ")" in p for p in parts):
            return m.group(0)                       # a subquery or an expression: leave it alone
        parsed = []
        for p in parts:
            im = _ITEM_RX.match(p)
            if im:
                parsed.append((im.group("name").strip().strip("\"`[]"), (im.group("alias") or "").strip()))
        if len(parsed) == len(parts):
            resolved = []
            for n, _a in parsed:
                if _norm(n) in ctes:                 # a name the statement defines itself: never a table
                    resolved.append(("", ""))
                else:
                    ref = _resolve_ref(wb, n)
                    if ref[1] == "near":
                        raise SqlError(f"there is no table '{n}'. The closest is {ref[0]} (handle {_handle_of(wb, ref[0])}): use its "
                                       f"exact name or its handle in FROM. All tables: "
                                       + ", ".join(f"{t['name']} = {t.get('handle')}" for t in wb.tables[:_MAX_HINT_NAMES]) + ".")
                    resolved.append(ref)
            if all(r for r, _k in resolved):
                out = []
                for (n, alias), (r, kind) in zip(parsed, resolved):
                    if kind == "guess":
                        guessed.append((n, r))
                    out.append(f'"{r}"' + (f" {alias}" if alias else ""))
                return f"{kw}{sep}" + ", ".join(out) + tail
        if len(parts) > 1 and len(parsed) < len(parts):
            # one name split by a stray comma ("BFSI_nov_20,2026_Sheet1") or a name sqlite cannot even read. Only when some part
            # is not a name at all: "Alpha_A a, Beta_B b" is two tables, and joining their names into one would drop a table.
            joined = [p.strip().strip("\"`[]") for p in parts]
            for joiner in ("_", ",", ""):
                cand, kind = _resolve_ref(wb, joiner.join(joined))
                if cand:
                    if kind == "guess":
                        guessed.append((", ".join(joined), cand))
                    return f'{kw}{sep}"{cand}"{tail}'
        return m.group(0)

    ctes = {_norm(c) for c in _CTE_RX.findall(sql)}   # the names the statement defines itself
    return _FROM_CLAUSE_RX.sub(clause, sql), guessed


def _known_names(wb: Workbook) -> set:
    names = set()
    for t in wb.tables:
        names.add(t["name"].lower())
        for c, _ in t["cols"]:
            names.add(c.lower())
    return names


# ---- filtering by a month -------------------------------------------------------------------------------------------
_KW_RX = re.compile(r"\b(where|group\s+by|order\s+by|limit|having|offset|union|except|intersect)\b", re.I)
_ISO_DAY_RX = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _keyword_positions(sql: str) -> list:
    """The keywords of the statement that are not inside parentheses: (start, end, keyword)."""
    out, depth, prev = [], 0, 0
    for m in _KW_RX.finditer(sql):
        depth += sql.count("(", prev, m.start()) - sql.count(")", prev, m.start())
        if depth == 0:
            out.append((m.start(), m.end(), re.sub(r"\s+", " ", m.group(1)).lower()))
        prev = m.end()
    return out


def add_where(sql: str, condition: str) -> str:
    """Put a condition into the statement's WHERE clause, before the grouping.

    The filter has to reach the rows, not the result of the aggregation: `SELECT Stage, SUM(value) FROM t GROUP BY Stage`
    filtered after the grouping would answer with one row, not with the November rows.
    """
    kws = _keyword_positions(sql)
    for start, end, kw in kws:
        if kw == "where":
            return sql[:end] + f" AND ({condition})" + sql[end:]
    for start, end, kw in kws:
        if kw in ("group by", "order by", "having", "limit", "offset"):
            return sql[:start] + f" WHERE ({condition}) " + sql[start:]
    return sql.rstrip() + f" WHERE ({condition})"


def date_columns(wb: Workbook) -> list:
    """The columns that hold dates: a column named like a date, or one whose values are dates.

    The sheet engine keeps a date column as ISO text ("2026-11-05"), so strftime() can read it.
    """
    out = []
    for t in wb.tables:
        for c, _ty in t["cols"]:
            if c in out:
                continue
            if "date" in c.lower():
                out.append(c)
                continue
            try:
                vals = [r[0] for r in wb.conn.execute(f'SELECT "{c}" FROM "{t["name"]}" LIMIT 20')]
            except sqlite3.Error:
                continue
            seen = [v for v in vals if v not in (None, "")]
            if seen and all(isinstance(v, str) and _ISO_DAY_RX.match(v.strip()) for v in seen):
                out.append(c)
    return out


def _unknown_quoted(wb: Workbook, sql: str) -> list:
    """Double-quoted names in the statement that are no table, no column, no alias and no CTE name.

    SQLite reads such a name as a text constant instead of failing, so a mistyped column ("Kam Name" for "KAM NAME")
    gives a plausible but wrong answer instead of an error. Names that stand where a value belongs (= "Dhaka") are
    text on purpose and are not reported.
    """
    aliases = {a.lower() for a in _ALIAS_RX.findall(sql)} | {a.lower() for a in _ALIAS_PLAIN_RX.findall(sql)}
    ctes = {c.lower() for c in _CTE_RX.findall(sql)}
    known = _known_names(wb) | aliases | ctes
    text = _ALIAS_RX.sub(" ", sql)
    text = _IN_LIST_RX.sub(" ", text)
    text = _CMP_LIT_RX.sub(" ", text)
    out = []
    for m in _QUOTED_RX.finditer(text):
        name = m.group(1)
        if _PATHY_RX.search(name):            # a json path or an expression, not a column that was typed wrong
            continue
        if text[:m.start()].rstrip().endswith("("):   # an argument of a function: a value, not a column
            continue
        if name.lower() not in known and name not in out:
            out.append(name)
    return out


def _explain(wb: Workbook, sql: str, msg: str) -> str:
    """sqlite's 'no such table/column' says nothing about what does exist. Name the tables and columns that are loaded,
    so a model that guessed a name corrects itself instead of guessing again."""
    names = [t["name"] for t in wb.tables]
    listed = ", ".join(names[:_MAX_HINT_NAMES])
    if len(names) > _MAX_HINT_NAMES:
        listed += f" (+{len(names) - _MAX_HINT_NAMES} more)"
    handles = ", ".join(f"{t.get('handle')} = {t['name']}" for t in wb.tables[:_MAX_HINT_NAMES]) or "(none)"
    m = re.search(r"no such table:\s*(.+)", msg, re.I)
    if m:
        wanted = m.group(1).strip().strip("\"`[]")
        close = difflib.get_close_matches(wanted, names, n=3, cutoff=0.4)
        out = [f"{msg}."]
        if close:   # with the handle, so the model can copy a name that exists instead of retyping it
            out.append("Did you mean: " + ", ".join(f"{c} (handle {_handle_of(wb, c)})" for c in close) + "?")
        out.append(f"Available tables: {listed or '(none)'}.")
        out.append(f"Or use the short handle of a table in FROM: {handles}.")
        out += [n + "." for n in wb.notes if "not loaded" in n]      # more tables exist than are loaded: the one wanted may be one
        out.append("Use those exact names (describe=true lists them with their columns).")
        return " ".join(out)
    m = re.search(r"no such column:\s*(.+)", msg, re.I)
    if m:
        wanted = m.group(1).strip().strip("\"`[]")
        low = sql.lower()
        owner, cols = "", []
        for t in wb.tables:                       # the table the statement reads (the longest name that fits)
            if len(t["name"]) > 2 and t["name"].lower() in low and len(t["name"]) > len(owner):
                owner, cols = t["name"], [c for c, _ in t["cols"]]
        out = [f"{msg}."]
        if cols:
            close = difflib.get_close_matches(wanted, cols, n=3, cutoff=0.4)
            if close:
                out.append("Did you mean: " + ", ".join(close) + "?")
            out.append(f"Columns of {owner}: " + ", ".join(f'"{c}"' for c in cols[:40]) + ".")
        else:
            out.append(f"Available tables: {listed or '(none)'}.")
        out.append("Use those exact names (describe=true lists them).")
        return " ".join(out)
    if re.search(r"unrecognized token|incomplete input|near \"|syntax error", msg, re.I):
        out = [f"{msg}.", "The statement does not parse; a table name that is not quoted and holds a space, a comma or a "
                           "digit is the usual cause.",
               f"Available tables: {listed or '(none)'}.",
               f"Or use the short handle of a table in FROM: {handles}."]
        named = [n for n in names if n.lower() in sql.lower()]
        if named:
            out.append("The statement mentions: " + ", ".join(named) + ".")
        out.append("Write the table name exactly and in double quotes (describe=true lists them with their columns).")
        return " ".join(out)
    return msg


def query(wb: Workbook, sql: str, limit: int = DEFAULT_ROWS) -> str:
    sql = str(sql or "").strip().rstrip(";").strip()
    if not sql:
        raise SqlError("sql required")
    if not re.match(r"(?is)^(select|with)\b", sql):
        raise SqlError("only SELECT queries are allowed (read-only)")
    sql, guessed = _rewrite_tables(wb, sql)          # a table name the model knows but spelled differently
    limit = max(1, min(int(limit or DEFAULT_ROWS), MAX_ROWS))
    deadline = time.monotonic() + QUERY_TIMEOUT_S
    with wb.lock:
        wb.conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 50000)
        try:
            cur = wb.conn.execute(sql)
            headers = [d[0] for d in cur.description or []]
            rows = cur.fetchmany(limit + 1)
        except sqlite3.OperationalError as e:
            msg = str(e)
            if "interrupted" in msg:
                raise SqlError(f"the query ran longer than {QUERY_TIMEOUT_S:.0f}s: filter earlier or select fewer rows")
            if "not authorized" in msg or "prohibited" in msg:
                raise SqlError("that statement is not allowed (read-only SELECT over the loaded tables)")
            raise SqlError(_explain(wb, sql, msg))
        except (sqlite3.Warning, sqlite3.ProgrammingError, sqlite3.DatabaseError) as e:
            raise SqlError("that statement is not allowed (read-only SELECT over the loaded tables)"
                           if ("authoriz" in str(e) or "prohibited" in str(e)) else str(e))
        finally:
            wb.conn.set_progress_handler(None, 0)
    more = len(rows) > limit
    rows = rows[:limit]
    out = ["| " + " | ".join(_fmt(h) for h in headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(_fmt(c) for c in r) + " |" for r in rows]
    text = "\n".join(out)
    text += f"\n({len(rows)} row{'s' if len(rows) != 1 else ''}{', more rows exist - add LIMIT/filters or aggregate' if more else ''})"
    if len(text) > MAX_OUT_CHARS:
        text = text[:MAX_OUT_CHARS].rsplit("\n", 1)[0] + "\n... (output cut)"
    unknown = _unknown_quoted(wb, sql)            # after the cap: the warning must survive a cut table
    if unknown:
        text += ("\nwarning: " + ", ".join(f'"{u}"' for u in unknown[:5]) + " is not a table or column of the loaded data. "
                 f"SQLite reads an unknown double-quoted name as the text '{unknown[0]}', so that part of the query counts a "
                 "constant instead of a column. Use the exact names from describe=true.")
    if guessed:                                   # the table was found by a near miss: say which one was read
        said = ", ".join(f"{w} -> {r}" for w, r in guessed[:3])
        text += f"\n(note: the table name was read as {said})"
    from ..pan import mask_pans
    return mask_pans(text)[0]
