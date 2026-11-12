"""Read-only SQL over the sheets of an attached workbook / CSV (stdlib sqlite3, in memory, no new dependency).

The model gets joins, pivots (conditional aggregates), window functions, medians / percentiles and month / quarter bucketing
for real analysis; it cannot change anything. The data is loaded into a private in-memory database; after loading the
connection is locked: `PRAGMA query_only`, and an authorizer that only lets SELECT statements read the loaded tables
(no ATTACH, PRAGMA, DDL, DML, load_extension). A progress handler stops queries that run longer than QUERY_TIMEOUT_S.
Output is capped (rows and characters) and card numbers are masked before it is returned.
"""
import datetime as _dt
import difflib
import math
import re
import sqlite3
import threading
import time
from collections import Counter, OrderedDict
from pathlib import Path

from ..file_tools.profile import column_stats, parse_number, plain_long_int, read_tables

QUERY_TIMEOUT_S = 8.0
DEFAULT_ROWS = 50
MAX_ROWS = 200
MAX_OUT_CHARS = 6000
CELL_WIDTH = 40
MAX_CACHED = 4

_SQLITE_RECURSIVE = 33
_ALLOWED_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, _SQLITE_RECURSIVE}
_DENIED_FUNCTIONS = {"load_extension", "readfile", "writefile", "edit", "fts3_tokenizer",
                     "randomblob", "zeroblob"}       # one call can allocate gigabytes; nothing here needs them
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
        h = str(h).replace('"', "'")              # a double quote in a header would end the quoted column name
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
            cols = _add_table(conn, tname, t.headers, t.rows)
            tables.append({"name": tname, "sheet": t.name, "file": fname, "cols": cols, "rows": t.total_rows,
                           "truncated": t.truncated, "formulas": dict(t.formulas), "preamble": list(t.preamble),
                           "aliases": [tname, t.name, fname, stem, f"{stem}_{t.name}", f"{fname}_{t.name}"]})
            if t.truncated:
                notes.append(f"{tname}: only the first {len(t.rows):,} of {t.total_rows:,} rows are loaded")
    _lock(conn)
    return Workbook(conn, tables, notes)


def from_tables(items: list) -> Workbook:
    """A Workbook from in-memory tables: [(name, source label, headers, rows)] or [(name, label, headers, rows, sheet)]
    (the knowledge base's sheets). Not cached: the rows behind it can change and are access-controlled by the caller.

    `name` becomes the SQL table name (made a safe identifier), `label` the document it came from and `sheet` the tab inside
    it - the model needs the sheet name to tell two tabs of the same document apart. `aliases` are the other names the
    table answers to (the document title, the file name, the sheet name); see _resolve_ref.
    """
    conn = _connect()
    used, tables = set(), []
    for item in items:
        name, label, headers, rows = item[0], item[1], item[2], item[3]
        sheet = item[4] if len(item) > 4 else name
        tname = _ident(name, used)
        cols = _add_table(conn, tname, headers, rows)
        aliases = [tname, name, sheet, label]
        if label and sheet and label != sheet:
            aliases.append(f"{label}_{sheet}")
        tables.append({"name": tname, "sheet": sheet, "file": label, "cols": cols, "rows": len(rows), "truncated": False,
                       "formulas": {}, "preamble": [], "aliases": aliases})
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


def _resolve_ref(wb: Workbook, ident: str) -> str:
    """The loaded table a FROM/JOIN reference names, or "" when nothing (or more than one thing) fits.

    A model writes the name it knows - the file name, with the space still a space or turned into a comma - and the exact
    identifier it must use is a mangled version of it. Rather than fail, the reference is matched against the names a
    table answers to (its identifier, its document title, its file name, its tab). The strongest match wins, and it only
    counts when exactly one table fits, so an ambiguous guess is still an error instead of a wrong answer.
    """
    ident = str(ident or "").strip().strip("\"`[]")
    if not ident:
        return ""
    low = ident.lower()
    exact = [t for t in wb.tables if t["name"].lower() == low]
    if len(exact) == 1:
        return exact[0]["name"]
    nr = _norm(ident)
    if len(nr) < 4:
        return ""
    best = {}
    for t in wb.tables:
        top = 0
        for a in t.get("aliases", ()):
            na = _norm(a)
            if not na:
                continue
            if nr == na:
                top = max(top, 3)                 # the very name
            elif len(nr) >= 6 and na.startswith(nr):
                top = max(top, 2)                 # the reference is the shorter form ("sales.xlsx" for "sales.xlsx_Sales")
            elif len(nr) >= 6 and nr.startswith(na):
                top = max(top, 1)                 # the reference carries more ("BFSI..._xlsx" for "BFSI...")
        if top:
            best[t["name"]] = top
    if not best:
        return ""
    top = max(best.values())
    hits = [n for n, s in best.items() if s == top]
    return hits[0] if len(hits) == 1 else ""


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


def _rewrite_tables(wb: Workbook, sql: str) -> str:
    """Point the FROM/JOIN references at the tables that are really loaded.

    Only a reference that names exactly one loaded table is rewritten, so this can never turn a query into a read of a
    different document. Everything else is left as written and sqlite reports it.
    """
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
                    resolved.append("")
                else:
                    resolved.append(_resolve_ref(wb, n))
            if all(resolved):
                out = []
                for (n, alias), r in zip(parsed, resolved):
                    out.append(f'"{r}"' + (f" {alias}" if alias else ""))
                return f"{kw}{sep}" + ", ".join(out) + tail
        if len(parts) > 1:
            # one name split by a stray comma ("BFSI_nov_20,2026_Sheet1") or a name sqlite cannot even read
            joined = [p.strip().strip("\"`[]") for p in parts]
            for joiner in ("_", ",", ""):
                cand = _resolve_ref(wb, joiner.join(joined))
                if cand:
                    return f'{kw}{sep}"{cand}"{tail}'
        return m.group(0)

    ctes = {_norm(c) for c in _CTE_RX.findall(sql)}   # the names the statement defines itself
    return _FROM_CLAUSE_RX.sub(clause, sql)


def _known_names(wb: Workbook) -> set:
    names = set()
    for t in wb.tables:
        names.add(t["name"].lower())
        for c, _ in t["cols"]:
            names.add(c.lower())
    return names


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
    m = re.search(r"no such table:\s*(.+)", msg, re.I)
    if m:
        wanted = m.group(1).strip().strip("\"`[]")
        close = difflib.get_close_matches(wanted, names, n=3, cutoff=0.4)
        out = [f"{msg}."]
        if close:
            out.append("Did you mean: " + ", ".join(close) + "?")
        out.append(f"Available tables: {listed or '(none)'}.")
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
               f"Available tables: {listed or '(none)'}."]
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
    sql = _rewrite_tables(wb, sql)                 # a table name the model knows but spelled differently
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
            raise SqlError(_explain(wb, sql, msg))
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
    unknown = _unknown_quoted(wb, sql)            # after the cap: the warning must survive a cut table
    if unknown:
        text += ("\nwarning: " + ", ".join(f'"{u}"' for u in unknown[:5]) + " is not a table or column of the loaded data. "
                 f"SQLite reads an unknown double-quoted name as the text '{unknown[0]}', so that part of the query counts a "
                 "constant instead of a column. Use the exact names from describe=true.")
    from ..pan import mask_pans
    return mask_pans(text)[0]
