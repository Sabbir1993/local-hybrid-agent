"""Exact queries over the spreadsheets in the knowledge base (counts, lists, group-by).

Semantic search is the wrong tool for "how many managers are there" or "list everyone in Dhaka": it returns
a handful of chunks and the model guesses the rest. Excel and CSV sources are therefore also kept as real
rows (parsed back from the marker text that core/kb_structure.py writes), and `run_query` answers from them
exactly. Rows live in memory.db next to the chunks and are removed with the source.

Queries are a small structured spec, never raw SQL: column names are mapped to positions and values are bound
parameters, so a model (or an injected instruction) cannot reach anything but the rows it is allowed to read.
"""

import difflib
import json
import re
from typing import Optional

MAX_LIMIT = 200
DEFAULT_LIMIT = 50
MAX_FILTERS = 8
MAX_IN_VALUES = 50
MAX_TABLES = 200

_ROW_RX = re.compile(r"^R(\d+): (.*)$")
_OPS = {"=", "!=", "contains", "starts_with", ">", ">=", "<", "<=", "in", "is_empty", "not_empty"}
_ready = False


def _db():
    from .memory.store import _db as memory_db
    return memory_db()


def _ensure() -> None:
    global _ready
    db = _db()
    if _ready and db.execute("SELECT 1 FROM sqlite_master WHERE name='kb_tables'").fetchone():
        return
    db.execute("""CREATE TABLE IF NOT EXISTS kb_tables (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL, sheet TEXT NOT NULL,
        columns TEXT NOT NULL, nrows INTEGER NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS kb_tables_source ON kb_tables(source_id)")
    db.execute("""CREATE TABLE IF NOT EXISTS kb_rows (
        table_id INTEGER NOT NULL, rownum INTEGER NOT NULL, cells TEXT NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS kb_rows_table ON kb_rows(table_id)")
    db.commit()
    _ready = True


# ------------------------------------------------------------------ parsing / storage

def parse_tables(text: str) -> list:
    """Marker text -> [{"sheet", "columns" (list or None), "rows": [(rownum, [cells])]}]."""
    tables, cur = [], None

    def start(sheet, cols=None):
        nonlocal cur
        cur = {"sheet": sheet, "columns": cols, "rows": []}
        tables.append(cur)

    sheet = ""
    for line in str(text or "").splitlines():
        if line.startswith("[Sheet: ") and line.rstrip().endswith("]"):
            sheet = line[8:].rstrip()[:-1]
            start(sheet)
        elif line.startswith("Columns: "):
            cols = line[9:].split(" | ")
            if cur is None or (cur["rows"] and cur["columns"] != cols):
                start(sheet, cols)
            elif cur["columns"] is None:
                cur["columns"] = cols
        else:
            m = _ROW_RX.match(line)
            if m:
                if cur is None:
                    start(sheet)
                cur["rows"].append((int(m.group(1)), m.group(2).split(" | ")))
    out = []
    for t in tables:
        if not t["rows"]:
            continue
        width = max(len(c) for _, c in t["rows"])
        cols = list(t["columns"] or [])
        cols += [f"Col{i + 1}" for i in range(len(cols), width)]
        out.append({"sheet": t["sheet"], "columns": cols[:max(width, len(cols))], "rows": t["rows"]})
    return out


def replace_source(source_id: int, text: str) -> int:
    """Store the spreadsheet rows of a source (replacing earlier ones). Returns the number of rows stored."""
    _ensure()
    delete_source(source_id)
    db = _db()
    total = 0
    for t in parse_tables(text)[:MAX_TABLES]:
        cur = db.execute("INSERT INTO kb_tables (source_id, sheet, columns, nrows) VALUES (?, ?, ?, ?)",
                         (source_id, t["sheet"], json.dumps(t["columns"]), len(t["rows"])))
        tid = cur.lastrowid
        db.executemany("INSERT INTO kb_rows (table_id, rownum, cells) VALUES (?, ?, ?)",
                       ((tid, n, json.dumps(cells)) for n, cells in t["rows"]))
        total += len(t["rows"])
    db.commit()
    return total


def delete_source(source_id: int) -> None:
    _ensure()
    db = _db()
    db.execute("DELETE FROM kb_rows WHERE table_id IN (SELECT id FROM kb_tables WHERE source_id=?)", (source_id,))
    db.execute("DELETE FROM kb_tables WHERE source_id=?", (source_id,))
    db.commit()


def list_tables(allowed_ids: set, titles: dict, source: str = "") -> list:
    _ensure()
    if not allowed_ids:
        return []
    q = ",".join("?" * len(allowed_ids))
    rows = _db().execute(f"SELECT id, source_id, sheet, columns, nrows FROM kb_tables WHERE source_id IN ({q}) "
                         "ORDER BY source_id, id", tuple(allowed_ids)).fetchall()
    out = []
    for tid, sid, sheet, cols, n in rows:
        title = titles.get(sid) or f"Source #{sid}"
        if source and source.lower() not in title.lower() and source.lower() not in str(sheet).lower():
            continue
        out.append({"id": tid, "source_id": sid, "title": title, "sheet": sheet, "columns": json.loads(cols), "nrows": n})
    return out


# ------------------------------------------------------------------ querying

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s).lower())


def _tokens(s: str) -> list:
    return re.findall(r"[a-z0-9]+", str(s).lower())


def _abbrev(t: str, w: str) -> bool:
    """`t` is a short form of the word `w`: same first letter and its letters appear in order ("dept" -> "department")."""
    if len(t) < 3 or t[0] != w[0]:
        return False
    it = iter(w)
    return all(ch in it for ch in t)


def _token_score(q: list, c: list) -> Optional[tuple]:
    """Every query token must be the start (or a short form) of a different word of the column name ("emp id" ->
    "Employee ID"). Score: more whole words first, then more word starts, then fewer unused words. None when it does not fit."""
    free, exact, starts = list(c), 0, 0
    for t in q:
        hit = next((w for w in free if w == t), None)
        if hit is not None:
            exact += 1
        else:
            hit = next((w for w in free if w.startswith(t)), None)
            if hit is not None:
                starts += 1
            else:
                hit = next((w for w in free if _abbrev(t, w)), None)
                if hit is None:
                    return None
        free.remove(hit)
    return exact, starts, -len(free)


def resolve_column(name: str, columns: list) -> Optional[int]:
    """Column position by exact (case-insensitive) name, else by a unique partial match, else by a unique best match on
    word beginnings. Never a guess between several."""
    n = _norm(name)
    if not n:
        return None
    exact = [i for i, c in enumerate(columns) if _norm(c) == n]
    if len(exact) == 1:
        return exact[0]
    part = [i for i, c in enumerate(columns) if n in _norm(c)]
    if len(part) == 1:
        return part[0]
    q = _tokens(name)
    if exact or part or not q:
        return None
    scored = [(sc, i) for i, c in enumerate(columns) if (sc := _token_score(q, _tokens(c))) is not None]
    if not scored:
        return None
    best = max(sc for sc, _ in scored)
    top = [i for sc, i in scored if sc == best]
    return top[0] if len(top) == 1 else None


def closest_columns(name: str, columns: list, n: int = 3) -> list:
    """Up to n real column names that look like `name` (for the 'no such column' message)."""
    q = _norm(name)
    by_norm = {}
    for c in columns:
        by_norm.setdefault(_norm(c), c)
    found = [by_norm[m] for m in difflib.get_close_matches(q, list(by_norm), n=n, cutoff=0.5)]
    qt = _tokens(name)
    for c in columns:
        if c not in found and qt and _token_score(qt, _tokens(c)) is not None:
            found.append(c)
    return found[:n]


def _like(v: str) -> str:
    return v.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _cond(op: str, idx: int, value):
    """(sql, params) for one filter on json position `idx`."""
    x = f"json_extract(cells, '$[{int(idx)}]')"
    if op == "is_empty":
        return f"({x} IS NULL OR trim({x}) = '')", []
    if op == "not_empty":
        return f"({x} IS NOT NULL AND trim({x}) <> '')", []
    if op == "in":
        vals = [str(v).lower() for v in (value if isinstance(value, list) else [value])][:MAX_IN_VALUES]
        return f"lower(trim({x})) IN ({','.join('?' * len(vals))})", vals
    v = str(value)
    if op == "=":
        return f"lower(trim({x})) = ?", [v.strip().lower()]
    if op == "!=":
        return f"(lower(trim({x})) <> ? OR {x} IS NULL)", [v.strip().lower()]
    if op == "contains":
        return f"lower({x}) LIKE ? ESCAPE '\\'", [f"%{_like(v.lower())}%"]
    if op == "starts_with":
        return f"lower(trim({x})) LIKE ? ESCAPE '\\'", [f"{_like(v.strip().lower())}%"]
    try:
        num = float(str(value).replace(",", ""))
    except ValueError:
        raise ValueError(f"'{op}' needs a number, got '{value}'")
    return f"CAST(replace({x}, ',', '') AS REAL) {op} ?", [num]


def run_query(spec: dict, allowed_ids: set, titles: dict) -> dict:
    """Answer a structured query. spec keys: source, sheet, filters [{column, op, value}], select [column],
    group_by column, count (bool), limit. Returns {"tables": [...], "groups": {...}|None, "total": int, "skipped": [...]}."""
    _ensure()
    filters = list(spec.get("filters") or [])[:MAX_FILTERS]
    select = [str(c) for c in (spec.get("select") or [])]
    group_by = spec.get("group_by")
    limit = max(1, min(int(spec.get("limit") or DEFAULT_LIMIT), MAX_LIMIT))
    for f in filters:
        if not isinstance(f, dict) or str(f.get("op") or "=") not in _OPS or not f.get("column"):
            raise ValueError("each filter needs a column and an op in: " + ", ".join(sorted(_OPS)))
    tables = list_tables(allowed_ids, titles, str(spec.get("source") or ""))
    if spec.get("sheet"):
        tables = [t for t in tables if str(spec["sheet"]).lower() in str(t["sheet"]).lower()]
    db = _db()
    result = {"tables": [], "groups": {} if group_by else None, "total": 0, "skipped": [], "missing": [], "available": [],
              "hint": ""}
    if not tables:                                         # tell the model what does exist instead of just "nothing"
        result["available"] = [_label(t) for t in list_tables(allowed_ids, titles, "")]
    searched = []
    for t in tables:
        cols = t["columns"]
        where, params, bad = ["table_id = ?"], [t["id"]], []
        for f in filters:
            idx = resolve_column(f["column"], cols)
            if idx is None:
                bad.append(str(f["column"]))
                continue
            sql, p = _cond(str(f.get("op") or "="), idx, f.get("value"))
            where.append(sql)
            params += p
        sel = [resolve_column(c, cols) for c in select]
        bad += [c for c, i in zip(select, sel) if i is None]
        gidx = resolve_column(group_by, cols) if group_by else None
        if group_by and gidx is None:
            bad.append(str(group_by))
        if bad:
            result["skipped"].append(_label(t))
            result["missing"].append({"table": _label(t), "names": list(dict.fromkeys(bad)), "columns": cols})
            continue
        w = " AND ".join(where)
        searched.append((t, filters))
        n = db.execute(f"SELECT COUNT(*) FROM kb_rows WHERE {w}", params).fetchone()[0]
        entry = {"title": t["title"], "sheet": t["sheet"], "count": n, "columns": [cols[i] for i in sel] if sel else cols,
                 "rows": []}
        result["total"] += n
        if group_by:
            for val, c in db.execute(f"SELECT trim(json_extract(cells, '$[{gidx}]')) AS v, COUNT(*) FROM kb_rows "
                                     f"WHERE {w} GROUP BY lower(v)", params):
                key = (val or "(empty)")
                result["groups"][key] = result["groups"].get(key, 0) + c
        elif not spec.get("count") and n:
            left = limit - sum(len(e["rows"]) for e in result["tables"])
            if left > 0:
                for rownum, cells in db.execute(f"SELECT rownum, cells FROM kb_rows WHERE {w} ORDER BY rownum LIMIT ?",
                                                params + [left]):
                    vals = json.loads(cells)
                    vals += [""] * (len(cols) - len(vals))
                    entry["rows"].append((rownum, [vals[i] for i in sel] if sel else vals))
        result["tables"].append(entry)
    eq = [f for f in filters if str(f.get("op") or "=") == "="]
    if result["total"] == 0 and eq:
        result["hint"] = _equals_hint(db, searched, eq)
    return result


def _label(t: dict) -> str:
    return f"{t['title']} / {t['sheet']}" if t["sheet"] else t["title"]


def _equals_hint(db, searched: list, eq: list) -> str:
    """Nothing equals the value: count (one cheap COUNT per searched table) how many rows contain it instead."""
    found = 0
    for t, filters in searched:
        where, params = ["table_id = ?"], [t["id"]]
        for f in filters:
            op = "contains" if f in eq else str(f.get("op") or "=")
            sql, p = _cond(op, resolve_column(f["column"], t["columns"]), f.get("value"))
            where.append(sql)
            params += p
        found += db.execute(f"SELECT COUNT(*) FROM kb_rows WHERE {' AND '.join(where)}", params).fetchone()[0]
    if not found:
        return ""
    vals = ", ".join(f"'{f.get('value')}'" for f in eq)
    return f'0 rows equal {vals}, but {found} rows contain it: use op "contains" (or "starts_with").'


def _missing_text(missing: list) -> str:
    """Which column name failed in which table, with that table's real columns and the closest names."""
    out = []
    for m in missing[:6]:
        cols = m["columns"]
        shown = " | ".join(cols[:40]) + (f" | ... {len(cols) - 40} more" if len(cols) > 40 else "")
        near = list(dict.fromkeys(c for name in m["names"] for c in closest_columns(name, cols)))[:3]
        names = ", ".join(f"'{n}'" for n in m["names"])
        line = f'No column {names} in "{m["table"]}". Its columns: {shown}.'
        line += (" Closest: " + ", ".join(f"'{c}'" for c in near) + ". Retry with one of these names."
                 if near else " Retry with one of its column names.")
        out.append(line)
    if len(missing) > 6:
        out.append(f"... {len(missing) - 6} more table(s) lack the named columns")
    return "\n".join(out)


def render(spec: dict, res: dict) -> str:
    if not res["tables"]:
        if res["missing"]:
            return _missing_text(res["missing"])
        if res.get("available"):
            what = " / ".join(f"'{spec[k]}'" for k in ("source", "sheet") if spec.get(k))
            av = res["available"]
            return (f"No spreadsheet matches {what or 'that request'}. Available: " + "; ".join(av[:20])
                    + (f"; ... {len(av) - 20} more" if len(av) > 20 else "") + ". Retry with part of one of these titles or sheets.")
        return "(no spreadsheet in the knowledge base matches that request)"
    out = []
    if res["groups"] is not None:
        out.append(f"Count by {spec.get('group_by')} ({res['total']} matching rows):")
        out.append("| value | count |\n|---|---|")
        for k, v in sorted(res["groups"].items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_LIMIT]:
            out.append(f"| {k} | {v} |")
    elif spec.get("count"):
        out.append(f"{res['total']} matching row(s).")
        for t in res["tables"]:
            out.append(f"- {t['title']}{' / ' + t['sheet'] if t['sheet'] else ''}: {t['count']}")
    else:
        out.append(f"{res['total']} matching row(s); showing up to {sum(len(t['rows']) for t in res['tables'])}.")
        for t in res["tables"]:
            if not t["rows"]:
                continue
            out.append(f"\n## {t['title']}{' / ' + t['sheet'] if t['sheet'] else ''} ({t['count']} rows)")
            out.append("| Row | " + " | ".join(t["columns"]) + " |")
            out.append("|---|" + "---|" * len(t["columns"]))
            for rownum, vals in t["rows"]:
                out.append(f"| R{rownum} | " + " | ".join(str(v).replace("|", "/") for v in vals) + " |")
        if res["total"] > sum(len(t["rows"]) for t in res["tables"]):
            out.append("\n(more rows match; narrow the filters or raise limit)")
    if res.get("hint"):
        out.append("\n" + res["hint"])
    if res.get("missing"):
        out.append("\nNot searched (no such column):\n" + _missing_text(res["missing"]))
    return "\n".join(out)


def describe(allowed_ids: set, titles: dict, source: str = "") -> str:
    ts = list_tables(allowed_ids, titles, source)
    if not ts:
        return "(no spreadsheet or CSV tables are available; only Excel and CSV sources are queryable)"
    out = [f"{len(ts)} table(s):"]
    for t in ts[:60]:
        out.append(f"- {t['title']}{' / ' + t['sheet'] if t['sheet'] else ''} ({t['nrows']} rows): " + " | ".join(t["columns"]))
    if len(ts) > 60:
        out.append(f"... {len(ts) - 60} more")
    return "\n".join(out)
