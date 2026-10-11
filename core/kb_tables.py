"""Exact queries over the spreadsheets in the knowledge base (counts, lists, group-by).

Semantic search is the wrong tool for "how many managers are there" or "list everyone in Dhaka": it returns
a handful of chunks and the model guesses the rest. Excel and CSV sources are therefore also kept as real
rows (parsed back from the marker text that core/kb_structure.py writes), and `run_query` answers from them
exactly. Rows live in memory.db next to the chunks and are removed with the source.

Queries are a small structured spec, never raw SQL: column names are mapped to positions and values are bound
parameters, so a model (or an injected instruction) cannot reach anything but the rows it is allowed to read.
"""

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


def resolve_column(name: str, columns: list) -> Optional[int]:
    """Column position by exact (case-insensitive) name, else by a unique partial match."""
    n = _norm(name)
    if not n:
        return None
    exact = [i for i, c in enumerate(columns) if _norm(c) == n]
    if len(exact) == 1:
        return exact[0]
    part = [i for i, c in enumerate(columns) if n in _norm(c)]
    return part[0] if len(part) == 1 else None


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
    result = {"tables": [], "groups": {} if group_by else None, "total": 0, "skipped": []}
    for t in tables:
        cols = t["columns"]
        where, params, ok = ["table_id = ?"], [t["id"]], True
        for f in filters:
            idx = resolve_column(f["column"], cols)
            if idx is None:
                ok = False
                break
            sql, p = _cond(str(f.get("op") or "="), idx, f.get("value"))
            where.append(sql)
            params += p
        sel = [resolve_column(c, cols) for c in select]
        gidx = resolve_column(group_by, cols) if group_by else None
        if not ok or any(i is None for i in sel) or (group_by and gidx is None):
            result["skipped"].append(f"{t['title']} / {t['sheet']}" if t["sheet"] else t["title"])
            continue
        w = " AND ".join(where)
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
    return result


def render(spec: dict, res: dict) -> str:
    if not res["tables"]:
        extra = f" ({len(res['skipped'])} table(s) lack the named columns)" if res["skipped"] else ""
        return "(no spreadsheet in the knowledge base matches that request" + extra + ")"
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
    if res["skipped"]:
        out.append(f"\nNot searched (no such column): {', '.join(res['skipped'][:5])}")
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
