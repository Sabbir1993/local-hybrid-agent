"""Read-only structured queries over an attached spreadsheet (xlsx / xls / csv): what a frontier harness's analysis tool
gives the model, without letting it run code on the server.

Operations: describe, filter, select columns, sort, group-by with aggregates, whole-table aggregates, distinct values,
row listing, count. Every result is capped (rows, cell width, characters) and states how many rows matched.
"""
import re
from collections import defaultdict
from pathlib import Path

from .profile import Table, parse_number, profile_file, read_tables

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
MAX_OUT_CHARS = 6000
CELL_WIDTH = 40
_FILTER_OPS = {"=", "==", "!=", "contains", "not_contains", "starts_with", ">", ">=", "<", "<=", "in", "is_empty", "not_empty"}
_AGG_FNS = {"count", "sum", "avg", "mean", "min", "max", "distinct"}


class QueryError(ValueError):
    pass


def _pick_table(tables: list, sheet) -> Table:
    if not tables:
        raise QueryError("the file has no data")
    if not sheet:
        return tables[0]
    key = str(sheet).strip().lower()
    for t in tables:
        if t.name.lower() == key:
            return t
    hits = [t for t in tables if key in t.name.lower()]
    if len(hits) == 1:
        return hits[0]
    raise QueryError(f"no sheet '{sheet}'. Sheets: " + ", ".join(t.name for t in tables))


def _col(t: Table, name) -> int:
    key = str(name or "").strip().lower()
    names = [h.lower() for h in t.headers]
    if key in names:
        return names.index(key)
    hits = [i for i, h in enumerate(names) if key and key in h]
    if len(hits) == 1:
        return hits[0]
    raise QueryError(f"no column '{name}'" + (" (ambiguous)" if hits else "") + ". Columns: " + ", ".join(t.headers[:60]))


def _text(v) -> str:
    return "" if v is None else re.sub(r"\s+", " ", str(v)).strip()


def _empty(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def _cmp_values(cell, op: str, value):
    """True/False for one filter; numbers compare as numbers when both sides parse, otherwise as text."""
    if op == "is_empty":
        return _empty(cell)
    if op == "not_empty":
        return not _empty(cell)
    if op == "in":
        vals = value if isinstance(value, list) else [value]
        return _text(cell).lower() in {_text(v).lower() for v in vals}
    a, b = parse_number(cell), parse_number(value)
    if op in (">", ">=", "<", "<="):
        if a is None or b is None:
            a, b = _text(cell).lower(), _text(value).lower()
            if not a:
                return False
        return {">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b}[op]
    ca, cb = _text(cell).lower(), _text(value).lower()
    if op in ("=", "=="):
        return (a == b) if (a is not None and b is not None) else ca == cb
    if op == "!=":
        return (a != b) if (a is not None and b is not None) else ca != cb
    if op == "contains":
        return cb in ca
    if op == "not_contains":
        return cb not in ca
    if op == "starts_with":
        return ca.startswith(cb)
    raise QueryError(f"unknown filter op '{op}' (use one of: {', '.join(sorted(_FILTER_OPS))})")


def _filtered(t: Table, filters) -> list:
    if not filters:
        return t.rows
    prepared = []
    for f in filters:
        if not isinstance(f, dict) or not f.get("column"):
            raise QueryError("each filter needs {column, op, value}")
        op = str(f.get("op") or "=").lower()
        if op not in _FILTER_OPS:
            raise QueryError(f"unknown filter op '{op}' (use one of: {', '.join(sorted(_FILTER_OPS))})")
        prepared.append((_col(t, f["column"]), op, f.get("value")))
    out = []
    for r in t.rows:
        r = r + [None] * (len(t.headers) - len(r))
        if all(_cmp_values(r[i], op, v) for i, op, v in prepared):
            out.append(r)
    return out


def _aggregate(rows: list, idx: int, fn: str):
    if fn == "count":
        return sum(1 for r in rows if idx < len(r) and not _empty(r[idx]))
    if fn == "distinct":
        return len({_text(r[idx]).lower() for r in rows if idx < len(r) and not _empty(r[idx])})
    nums = [n for n in (parse_number(r[idx]) for r in rows if idx < len(r)) if n is not None]
    if not nums:
        return None
    return {"sum": sum(nums), "avg": sum(nums) / len(nums), "mean": sum(nums) / len(nums), "min": min(nums), "max": max(nums)}[fn]


def _fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:,.2f}" if abs(v) < 1e15 and not v.is_integer() else f"{v:,.0f}"
    s = _text(v)
    return s if len(s) <= CELL_WIDTH else s[: CELL_WIDTH - 1] + "…"


def _md(headers: list, rows: list) -> str:
    out = ["| " + " | ".join(_fmt(h) for h in headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(_fmt(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def _cap(text: str) -> str:
    return text if len(text) <= MAX_OUT_CHARS else text[:MAX_OUT_CHARS].rsplit("\n", 1)[0] + "\n... (output cut)"


def _sort_key(v):
    n = parse_number(v)
    return (0, n, "") if n is not None else (1, 0.0, _text(v).lower())


def run_data_query(path, args: dict) -> str:
    """Answer one data_query call against the file at `path`. Raises QueryError for a bad request."""
    p = Path(path)
    if args.get("describe"):
        text, _ = profile_file(p, max_chars=MAX_OUT_CHARS)
        return text
    tables = read_tables(p)
    t = _pick_table(tables, args.get("sheet"))
    rows = _filtered(t, args.get("filters"))
    note = f"Sheet \"{t.name}\": {len(rows):,} of {t.total_rows:,} rows match"
    if t.truncated:
        note += f" (only the first {len(t.rows):,} rows of this sheet are loaded)"
    limit = max(1, min(int(args.get("limit") or DEFAULT_LIMIT), MAX_LIMIT))

    group = args.get("group_by")
    aggs = args.get("agg") or []
    if isinstance(aggs, dict):
        aggs = [aggs]
    for a in aggs:
        if not isinstance(a, dict) or str(a.get("fn", "")).lower() not in _AGG_FNS:
            raise QueryError("each agg needs {fn: count|sum|avg|min|max|distinct, column}")
    if group:
        gi = _col(t, group)
        aggs = aggs or [{"fn": "count"}]
        buckets = defaultdict(list)
        for r in rows:
            buckets[_text(r[gi]) or "(empty)"].append(r)
        specs = [(str(a["fn"]).lower(), _col(t, a["column"]) if a.get("column") else gi) for a in aggs]
        heads = [t.headers[gi]] + [f"{fn}({t.headers[i]})" if a.get("column") else fn for (fn, i), a in zip(specs, aggs)]
        table = [[k] + [_aggregate(v, i, fn) if (fn != "count" or a.get("column")) else len(v) for (fn, i), a in zip(specs, aggs)]
                 for k, v in buckets.items()]
        sort_idx = 1 if not args.get("sort_by") else (heads.index(args["sort_by"]) if args["sort_by"] in heads else 1)
        table.sort(key=lambda r: _sort_key(r[sort_idx]), reverse=args.get("desc", True) is not False)
        return _cap(f"{note}; {len(table):,} groups by {t.headers[gi]}\n" + _md(heads, table[:limit]) +
                    (f"\n... {len(table) - limit:,} more groups (raise limit up to {MAX_LIMIT})" if len(table) > limit else ""))
    if aggs:
        heads, vals = [], []
        for a in aggs:
            fn = str(a["fn"]).lower()
            idx = _col(t, a["column"]) if a.get("column") else 0
            heads.append(f"{fn}({t.headers[idx]})" if a.get("column") else "rows")
            vals.append(len(rows) if (fn == "count" and not a.get("column")) else _aggregate(rows, idx, fn))
        return f"{note}\n" + _md(heads, [vals])
    if args.get("distinct"):
        di = _col(t, args["distinct"])
        counts = defaultdict(int)
        for r in rows:
            counts[_text(r[di]) or "(empty)"] += 1
        ordered = sorted(counts.items(), key=lambda kv: -kv[1])
        return _cap(f"{note}; {len(ordered):,} distinct values of {t.headers[di]}\n" +
                    _md([t.headers[di], "rows"], [[k, c] for k, c in ordered[:limit]]) +
                    (f"\n... {len(ordered) - limit:,} more" if len(ordered) > limit else ""))
    if args.get("count"):
        return note
    sel = [_col(t, c) for c in args["select"]] if args.get("select") else list(range(min(len(t.headers), 12)))
    if args.get("sort_by"):
        si = _col(t, args["sort_by"])
        rows = sorted(rows, key=lambda r: _sort_key(r[si] if si < len(r) else None), reverse=bool(args.get("desc")))
    shown = rows[:limit]
    body = _md([t.headers[i] for i in sel], [[(r[i] if i < len(r) else None) for i in sel] for r in shown])
    return _cap(f"{note}\n{body}" + (f"\n... {len(rows) - limit:,} more rows (narrow with filters, or raise limit up to {MAX_LIMIT})"
                                     if len(rows) > limit else ""))
