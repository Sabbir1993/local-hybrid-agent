"""Read-only SQL over the sheets of an attached workbook / CSV (stdlib sqlite3, in memory, no new dependency).

The model gets joins, pivots (conditional aggregates), window functions, medians / percentiles and month / quarter bucketing
for real analysis; it cannot change anything. The data is loaded into a private in-memory database; after loading the
connection is locked: `PRAGMA query_only`, and an authorizer that only lets SELECT statements read the loaded tables
(no ATTACH, PRAGMA, DDL, DML, load_extension). A progress handler stops queries that run longer than QUERY_TIMEOUT_S.
Output is capped (rows and characters) and card numbers are masked before it is returned.
"""
import datetime as _dt
import math
import re
import sqlite3
import threading
import time
from collections import Counter, OrderedDict
from pathlib import Path

from ..file_tools.profile import column_stats, parse_number, read_tables

QUERY_TIMEOUT_S = 8.0
DEFAULT_ROWS = 50
MAX_ROWS = 200
MAX_OUT_CHARS = 6000
CELL_WIDTH = 40
MAX_CACHED = 4

_SQLITE_RECURSIVE = 33
_ALLOWED_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, _SQLITE_RECURSIVE}
_DENIED_FUNCTIONS = {"load_extension", "readfile", "writefile", "edit", "fts3_tokenizer"}
_DMY = re.compile(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})$")


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


def _convert(kind: str, v):
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if kind == "number":
        return parse_number(v)
    if kind == "date":
        return _iso(v)
    return v if isinstance(v, (int, float)) else str(v)


class Workbook:
    """A loaded file: the sqlite connection plus what the model needs to know to write queries."""

    def __init__(self, conn, tables: list, notes: list):
        self.conn, self.tables, self.notes = conn, tables, notes     # tables: [{name, sheet, file, cols, rows, ...}]
        self.lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.create_aggregate("MEDIAN", 1, _Median)
    conn.create_aggregate("STDEV", 1, _Stdev)
    conn.create_aggregate("PERCENTILE", 2, _Percentile)
    conn.create_aggregate("MODE", 1, _Mode)
    return conn


def _add_table(conn, tname: str, headers: list, rows: list) -> list:
    """Create `tname` and fill it; each column becomes REAL when it is mostly numbers, else TEXT (dates as ISO text).
    -> [(column, type)]"""
    cols, kinds, seen = [], [], set()
    data = list(zip(*[(r + [None] * len(headers))[: len(headers)] for r in rows])) if rows else [[] for _ in headers]
    for h, values in zip(headers, data):
        kind = column_stats(list(values), h)["kind"]
        kinds.append(kind)
        cname = h if h.lower() not in seen else f"{h}_{len(seen)}"
        seen.add(cname.lower())
        cols.append((cname, "REAL" if kind == "number" else "TEXT"))
    conn.execute(f'CREATE TABLE "{tname}" (' + ", ".join(f'"{c}" {ty}' for c, ty in cols) + ")")
    width = len(kinds)
    conn.executemany(f'INSERT INTO "{tname}" VALUES (' + ",".join("?" * width) + ")",
                     (tuple(_convert(k, v) for k, v in zip(kinds, (r + [None] * width)[:width])) for r in rows))
    return cols


def _lock(conn) -> None:
    conn.commit()
    conn.execute("PRAGMA query_only = ON")
    conn.set_authorizer(_authorizer)


def _build(paths: list) -> Workbook:
    conn = _connect()
    used, tables, notes = set(), [], []
    multi = len(paths) > 1
    for path in paths:
        stem = _ident(Path(path).stem, set())
        for t in read_tables(path):
            tname = _ident(f"{stem}_{t.name}" if multi else t.name, used)
            cols = _add_table(conn, tname, t.headers, t.rows)
            tables.append({"name": tname, "sheet": t.name, "file": Path(path).name, "cols": cols, "rows": t.total_rows,
                           "truncated": t.truncated, "formulas": dict(t.formulas), "preamble": list(t.preamble)})
            if t.truncated:
                notes.append(f"{tname}: only the first {len(t.rows):,} of {t.total_rows:,} rows are loaded")
    _lock(conn)
    return Workbook(conn, tables, notes)


def from_tables(items: list) -> Workbook:
    """A Workbook from in-memory tables: [(name, source label, headers, rows)] (the knowledge base's sheets). Not cached:
    the rows behind it can change and are access-controlled by the caller."""
    conn = _connect()
    used, tables = set(), []
    for name, label, headers, rows in items:
        tname = _ident(name, used)
        cols = _add_table(conn, tname, headers, rows)
        tables.append({"name": tname, "sheet": name, "file": label, "cols": cols, "rows": len(rows), "truncated": False,
                       "formulas": {}, "preamble": []})
    _lock(conn)
    return Workbook(conn, tables, [])


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
    lines = ["Tables (use these exact names, double-quote columns with spaces; dates are ISO text YYYY-MM-DD, numbers are REAL):"]
    for t in wb.tables:
        lines.append(f'- {t["name"]}  ({t["rows"]:,} rows, sheet "{t["sheet"]}" of {t["file"]})')
        lines.append("    " + ", ".join(f'"{c}" {ty}' for c, ty in t["cols"][:60]) + (" ..." if len(t["cols"]) > 60 else ""))
        if t["formulas"]:
            lines.append("    formula columns: " + ", ".join(f"{h} ({f})" for h, f in list(t["formulas"].items())[:6]))
        if t["preamble"]:
            lines.append("    title rows above the header (ignored): " + " | ".join(t["preamble"][:2]))
    lines += wb.notes
    lines.append("Extra functions: MEDIAN(x), STDEV(x), PERCENTILE(x, 0.9), MODE(x); strftime('%Y-%m', date_col) for month, "
                 "'%Y' year; window functions and CTEs work. SELECT only.")
    return "\n".join(lines)


def _fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:,.2f}" if abs(v) < 1e15 and not v.is_integer() else f"{v:,.0f}"
    s = re.sub(r"\s+", " ", str(v)).strip()
    return s if len(s) <= CELL_WIDTH else s[: CELL_WIDTH - 1] + "…"


def query(wb: Workbook, sql: str, limit: int = DEFAULT_ROWS) -> str:
    sql = str(sql or "").strip().rstrip(";").strip()
    if not sql:
        raise SqlError("sql required")
    if not re.match(r"(?is)^(select|with)\b", sql):
        raise SqlError("only SELECT queries are allowed (read-only)")
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
            if "not authorized" in msg:
                raise SqlError("that statement is not allowed (read-only SELECT over the loaded tables)")
            raise SqlError(msg)
        except (sqlite3.Warning, sqlite3.ProgrammingError, sqlite3.DatabaseError) as e:
            raise SqlError("that statement is not allowed (read-only SELECT)" if "authoriz" in str(e) else str(e))
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
    from ..pan import mask_pans
    return mask_pans(text)[0]
