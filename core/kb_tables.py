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

from .pan import ingest_mask_enabled, is_card_header, mask_cell, mask_split_pan_cells, row_may_split_pan

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


def _with_mask(db):
    """The connection of this thread with the masking function the filters use (see _text). It can only hide a card number."""
    from .sheet_engine.sql import _sql_mask, _sql_mask_card
    c = db.conn() if hasattr(db, "conn") else db
    c.create_function("kb_mask", 1, _sql_mask, deterministic=True)
    c.create_function("kb_mask_card", 1, _sql_mask_card, deterministic=True)
    return db


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
    # masked: 1 = the rows were stored with card numbers masked (knowledge.mask_pan_at_ingest); 0 = stored as they came, which is
    # every table of a version before this column. Added to an existing database by ALTER, guarded by a column check.
    if "masked" not in [r[1] for r in db.execute("PRAGMA table_info(kb_tables)")]:
        db.execute("ALTER TABLE kb_tables ADD COLUMN masked INTEGER NOT NULL DEFAULT 0")
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


def _remasked_row(js: str, card_cols: list) -> str:
    """One stored row with its card numbers masked (remask_all): inside a cell, then split over neighbouring cells."""
    cells = [mask_cell(c, card_cols[i] if i < len(card_cols) else False) if len(c) >= 13 else c
             for i, c in enumerate(json.loads(js))]
    cells = mask_split_pan_cells(cells)
    return json.dumps(cells)


def load_row(js: str) -> list:
    """The cells of a stored row for reading (kb_query output, the kb_sql copy path), with a card number that an older version
    stored split over neighbouring cells hidden too: the same check as at storing, so every path agrees."""
    cells = json.loads(js)
    return mask_split_pan_cells(cells) if row_may_split_pan(js) else cells


def replace_source(source_id: int, text: str) -> int:
    """Store the spreadsheet rows of a source (replacing earlier ones). Returns the number of rows stored.

    Card numbers are masked here, once, so no query has to find them later (PCI DSS 3.5): a number split over neighbouring
    cells always, one inside a cell when knowledge.mask_pan_at_ingest is on (the default). kb_structure.mask_text does it on
    the marker text before parsing; it only touches the few rows that can hold a card number, so a big sheet costs no more."""
    from .kb_structure import mask_text
    _ensure()
    delete_source(source_id)
    db = _db()
    ingest = ingest_mask_enabled()
    text = mask_text(text, single=ingest)
    total = 0
    for t in parse_tables(text)[:MAX_TABLES]:
        cur = db.execute("INSERT INTO kb_tables (source_id, sheet, columns, nrows, masked) VALUES (?, ?, ?, ?, ?)",
                         (source_id, t["sheet"], json.dumps(t["columns"]), len(t["rows"]), int(ingest)))
        tid = cur.lastrowid
        db.executemany("INSERT INTO kb_rows (table_id, rownum, cells) VALUES (?, ?, ?)",
                       ((tid, n, json.dumps(cells)) for n, cells in t["rows"]))
        total += len(t["rows"])
    db.commit()
    return total


def unmasked_sources() -> int:
    """How many sources have a table that was stored before card numbers were masked at ingest (see remask_all)."""
    _ensure()
    return _db().execute("SELECT COUNT(DISTINCT source_id) FROM kb_tables WHERE masked = 0").fetchone()[0]


def remask_all() -> dict:
    """Apply the ingest masking to what is already stored, in place, without the original files: every row of a table that is
    not marked masked yet, and the text of the knowledge chunks (and so the full-text index, through its triggers).
    Safe to run again (masked text stays as it is). Returns {"tables", "rows", "chunks"} changed.

    The stored chunk vectors were computed from the old text and are not touched, and a number cut in two by a chunk boundary
    is not seen here: re-index the source (POST /knowledge/{id}/reindex, permission knowledge.manage) to rebuild its chunks
    and vectors from the masked text."""
    from .kb_structure import mask_text
    _ensure()
    db = _db()
    done = {"tables": 0, "rows": 0, "chunks": 0}
    for tid, cols in db.execute("SELECT id, columns FROM kb_tables WHERE masked = 0").fetchall():
        card_cols = [is_card_header(c) for c in json.loads(cols)]
        changed = []
        for rid, cells in db.execute("SELECT rowid, cells FROM kb_rows WHERE table_id = ?", (tid,)):
            js = _remasked_row(cells, card_cols)
            if js != cells:
                changed.append((js, rid))
        if changed:
            db.executemany("UPDATE kb_rows SET cells = ? WHERE rowid = ?", changed)
        db.execute("UPDATE kb_tables SET masked = 1 WHERE id = ?", (tid,))
        done["tables"] += 1
        done["rows"] += len(changed)
    swaps = []
    has_chunks = db.execute("SELECT 1 FROM sqlite_master WHERE name = 'chunks'").fetchone()
    for cid, text in (db.execute("SELECT id, text FROM chunks WHERE source = 'knowledge'").fetchall() if has_chunks else []):
        masked = mask_text(text)
        if masked != text:
            swaps.append((masked, cid))
    if swaps:
        db.executemany("UPDATE chunks SET text = ? WHERE id = ?", swaps)   # the chunks_fts_au trigger keeps the full-text index in step
    done["chunks"] = len(swaps)
    db.commit()
    return done


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
    """`t` is a short form of the word `w`: at least 4 letters, the same first letter and its letters appear in order
    ("dept" -> "department"). Shorter ones ("amt", "id", "dt") fit far too many words."""
    if len(t) < 4 or t[0] != w[0]:
        return False
    it = iter(w)
    return all(ch in it for ch in t)


def _token_score(q: list, c: list, short_forms: bool = True) -> Optional[tuple]:
    """Every query token must be a whole word of the column name, or the start (3+ letters) of one, or a short form of one
    ("emp id" -> "Employee ID"); each word of the column is used once. Score: more whole words first, then more word starts,
    then fewer unused words. None when it does not fit. Never a match in the middle of a word ("id" is not in "Paid")."""
    free, exact, starts = list(c), 0, 0
    for t in q:
        hit = next((w for w in free if w == t), None)
        if hit is not None:
            exact += 1
        else:
            hit = next((w for w in free if len(t) >= 3 and w.startswith(t)), None)
            if hit is not None:
                starts += 1
            else:
                hit = next((w for w in free if short_forms and _abbrev(t, w)), None) if short_forms else None
                if hit is None:
                    return None
        free.remove(hit)
    return exact, starts, -len(free)


def resolve_column(name: str, columns: list) -> Optional[int]:
    """Column position: the exact name (case and punctuation ignored); else the one column that has every word of `name` as a
    whole word; else the one column whose words start with them ("emp id" -> "Employee ID"); else a short form that fits one
    ("dept" -> "Department"). Never a part of a word ("id" is not "Paid Amount", "total" is not "Subtotal") and never a guess
    between several: None, and the caller lists the real columns. A name that is the longer form of another column's word
    ("customer" next to "Cust Name") is not answered either - it could mean that column."""
    n = _norm(name)
    if not n:
        return None
    exact = [i for i, c in enumerate(columns) if _norm(c) == n]
    if exact:
        return exact[0] if len(exact) == 1 else None
    q = _tokens(name)
    if not q:
        return None
    toks = [_tokens(c) for c in columns]
    whole = [i for i, c in enumerate(toks) if all(t in c for t in q) and len(set(q)) == len(q)]
    if whole:
        found = whole[0] if len(whole) == 1 else None
    else:
        scored = [(sc, i) for i, c in enumerate(toks) if (sc := _token_score(q, c)) is not None]
        found = None
        if scored:
            best = max(sc for sc, _ in scored)
            top = [i for sc, i in scored if sc == best]
            found = top[0] if len(top) == 1 else None
    if found is not None and len(q) == 1 and len(q[0]) >= 4 and any(
            j != found and any(len(w) >= 3 and w != q[0] and q[0].startswith(w) for w in toks[j]) for j in range(len(columns))):
        return None
    return found


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


def _num_value(value) -> Optional[float]:
    from .sheet_engine.sql import parse_cell_number
    return parse_cell_number(value if isinstance(value, (int, float)) and not isinstance(value, bool) else str(value))


def _text(x: str, card: bool = False) -> str:
    """The cell as a filter sees it: a card number masked, exactly as the SQL tool and the output show it. A filter on the raw
    digits ("starts_with 4111", "contains 8888") would otherwise tell a model which digits a card number has, one question at a time."""
    from .sheet_engine.sql import masked_text
    return masked_text(x, card_column=card)


def _cond(op: str, idx: int, value, card: bool = False):
    """(sql, params) for one filter on json position `idx`; `card`: the column is named like a card column."""
    from .sheet_engine.sql import num_expr
    x = f"json_extract(cells, '$[{int(idx)}]')"
    t = _text(x, card)
    if op == "is_empty":
        return f"({x} IS NULL OR trim({x}) = '')", []
    if op == "not_empty":
        return f"({x} IS NOT NULL AND trim({x}) <> '')", []
    if op == "in":
        vals = [str(v).lower() for v in (value if isinstance(value, list) else [value])][:MAX_IN_VALUES]
        return f"lower(trim({t})) IN ({','.join('?' * len(vals))})", vals
    v = str(value)
    if op in ("=", "!="):
        eq, want = f"lower(trim({t})) = ?", [v.strip().lower()]
        num = _num_value(value)
        if num is not None:                                # 1200 also finds "1,200", "$1,200" and "1200.0"
            eq, want = f"({eq} OR COALESCE({num_expr(x)} = ?, 0))", want + [num]
        if op == "=":
            return eq, want
        return f"({x} IS NULL OR NOT {eq})", want
    if op == "contains":
        return f"lower({t}) LIKE ? ESCAPE '\\'", [f"%{_like(v.lower())}%"]
    if op == "starts_with":
        return f"lower(trim({t})) LIKE ? ESCAPE '\\'", [f"{_like(v.strip().lower())}%"]
    num = _num_value(value)
    if num is None:
        if len(re.sub(r"\D", "", v)) >= 13:
            raise ValueError(f"'{op}' cannot compare a value of 13+ digits (a card or account number is an identifier, not an amount)")
        raise ValueError(f"'{op}' needs a number, got '{value}'")
    return f"{num_expr(x)} {op} ?", [num]                  # a blank, "N/A" or text cell is NULL here, so it is never in range


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
    db = _with_mask(_db())
    result = {"tables": [], "groups": {} if group_by else None, "total": 0, "skipped": [], "missing": [], "available": [],
              "hint": "", "matched": [], "group_sources": {}}
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
            sql, p = _cond(str(f.get("op") or "="), idx, f.get("value"), is_card_header(cols[idx]))
            where.append(sql)
            params += p
        sel = [resolve_column(c, cols) for c in select]
        bad += [c for c, i in zip(select, sel) if i is None]
        gidx = resolve_column(group_by, cols) if group_by else None
        if group_by and gidx is None:
            bad.append(str(group_by))
        if not bad:                                        # name the column that answered, when it was not the name asked for
            asked = [(str(f["column"]), resolve_column(f["column"], cols)) for f in filters] + list(zip(select, sel))
            asked += [(str(group_by), gidx)] if group_by else []
            for q, i in asked:
                pair = (q, cols[i])
                if _norm(q) != _norm(cols[i]) and pair not in result["matched"]:
                    result["matched"].append(pair)
        if bad:
            result["skipped"].append(_label(t))
            result["missing"].append({"table": _label(t), "names": list(dict.fromkeys(bad)), "columns": cols})
            continue
        w = " AND ".join(where)
        searched.append((t, filters))
        n = db.execute(f"SELECT COUNT(*) FROM kb_rows WHERE {w}", params).fetchone()[0]
        entry = {"title": t["title"], "sheet": t["sheet"], "source_id": t["source_id"], "count": n, "columns": [cols[i] for i in sel] if sel else cols,
                 "rows": []}
        result["total"] += n
        if group_by:
            gx = _text(f"json_extract(cells, '$[{gidx}]')", is_card_header(cols[gidx]))
            for val, c in db.execute(f"SELECT trim({gx}) AS v, COUNT(*) FROM kb_rows WHERE {w} GROUP BY lower(v)", params):
                key = (val or "(empty)")
                result["groups"][key] = result["groups"].get(key, 0) + c
                result["group_sources"].setdefault(key, set()).add(t["source_id"])
        elif not spec.get("count") and n:
            left = limit - sum(len(e["rows"]) for e in result["tables"])
            if left > 0:
                for rownum, cells in db.execute(f"SELECT rownum, cells FROM kb_rows WHERE {w} ORDER BY rownum LIMIT ?",
                                                params + [left]):
                    vals = load_row(cells)
                    vals += [""] * (len(cols) - len(vals))
                    pick = sel if sel else list(range(len(cols)))
                    entry["rows"].append((rownum, [mask_cell(vals[i], is_card_header(cols[i])) for i in pick]))
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
            ci = resolve_column(f["column"], t["columns"])
            sql, p = _cond(op, ci, f.get("value"), is_card_header(t["columns"][ci]))
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
    if res.get("matched"):                                 # the name was not exact: say which column was really used
        out.append("\n(" + "; ".join(f"matched '{q}' -> '{c}'" for q, c in res["matched"][:6]) + ")")
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
