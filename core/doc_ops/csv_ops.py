"""CSV: outline + in-place edit ops that keep untouched records byte-identical.

Addresses: `r3c2` (row 3, column 2, 1-based, header is row 1), `r3cAmount`
(column by header name), `B3` (A1 style).
"""

import csv
import io
import re

from .base import DocOpError, EditResult, Snapshot, digest, op_arg, require

MAX_ROWS_SHOWN = 400
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
_NUM = re.compile(r"^-?\d+(\.\d+)?$")


class _Rec:
    """One CSV record: parsed fields plus the exact source text (kept when untouched)."""
    __slots__ = ("fields", "raw")

    def __init__(self, fields, raw):
        self.fields, self.raw = fields, raw


def _decode(data: bytes) -> tuple[str, str]:
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = data.decode(enc)
            if enc == "utf-8-sig" and not data.startswith(b"\xef\xbb\xbf"):
                enc = "utf-8"
            return text, enc
        except UnicodeDecodeError:
            continue
    raise DocOpError("cannot decode CSV")


def _parse(data: bytes):
    text, enc = _decode(data)
    sample = text[:20000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    newline = "\r\n" if "\r\n" in sample else "\n"
    recs, buf = [], ""
    q = dialect.quotechar or '"'
    for line in text.splitlines(keepends=True):
        buf += line
        if buf.count(q) % 2:   # a quoted field continues on the next line
            continue
        fields = next(csv.reader([buf.rstrip("\r\n")], dialect), [])
        recs.append(_Rec(fields, buf))
        buf = ""
    if buf:
        recs.append(_Rec(next(csv.reader([buf.rstrip("\r\n")], dialect), []), buf))
    trailing_nl = text.endswith(("\n", "\r"))
    return recs, dialect, enc, newline, trailing_nl


def inspect(data: bytes) -> dict:
    recs, dialect, enc, _nl, _t = _parse(data)
    head = f"CSV: {len(recs)} rows, delimiter {dialect.delimiter!r}, encoding {enc}"
    if recs:
        head += f", header: {recs[0].fields}"
    els = []
    for i, r in enumerate(recs[:MAX_ROWS_SHOWN], 1):
        els.append({"addr": f"r{i}", "kind": "row", "text": " | ".join(r.fields)})
    if len(recs) > MAX_ROWS_SHOWN:
        els.append({"addr": f"r{MAX_ROWS_SHOWN + 1}", "kind": "more", "text": f"... {len(recs) - MAX_ROWS_SHOWN} more rows"})
    return {"kind": "csv", "header": head, "elements": els}


def _safe(v) -> str:
    """Neutralise spreadsheet formula injection in values written from model text."""
    s = "" if v is None else str(v)
    if s.startswith(_FORMULA_START) and not _NUM.match(s):
        return "'" + s
    return s


def _col_of(recs, token: str) -> int:
    if token.isdigit():
        return int(token)
    header = [h.strip().lower() for h in (recs[0].fields if recs else [])]
    if token.strip().lower() in header:
        return header.index(token.strip().lower()) + 1
    if re.fullmatch(r"[A-Za-z]{1,3}", token):
        from .xlsx_ops import col_to_num
        return col_to_num(token)
    raise DocOpError(f"unknown column '{token}' (header: {recs[0].fields if recs else []})")


def _addr(recs, addr: str) -> tuple[int, int]:
    a = str(addr).strip()
    m = re.fullmatch(r"r(\d+)c(.+)", a, re.I)
    if m:
        return int(m.group(1)), _col_of(recs, m.group(2))
    m = re.fullmatch(r"([A-Za-z]{1,3})(\d+)", a)
    if m:
        from .xlsx_ops import col_to_num
        return int(m.group(2)), col_to_num(m.group(1))
    raise DocOpError(f"bad CSV address '{addr}' (use r3c2, r3cAmount or B3)")


def apply(data: bytes, ops: list[dict]) -> EditResult:
    recs, dialect, enc, nl, trailing = _parse(data)
    snap = Snapshot(lambda r: digest(repr(r.fields).encode()))
    snap.take((f"r{i}", r) for i, r in enumerate(recs, 1))
    changes = []

    def touch(rec):
        snap.touch(rec)
        rec.raw = None   # re-serialise this record

    for op in ops:
        kind = str(op.get("op", "")).lower()
        if kind == "set_cell":
            r, c = _addr(recs, op_arg(op, "addr", "cell", "target"))
            require(1 <= r <= len(recs), f"row {r} does not exist ({len(recs)} rows)")
            rec = recs[r - 1]
            rec.fields += [""] * (c - len(rec.fields))
            rec.fields[c - 1] = _safe(op.get("value", op.get("text")))
            touch(rec)
            changes.append(f"r{r}c{c} = {rec.fields[c - 1]}")
        elif kind == "set_row":
            r = int(str(op_arg(op, "row", "addr")).lstrip("rR"))
            require(1 <= r <= len(recs), f"row {r} does not exist")
            recs[r - 1].fields = [_safe(v) for v in op_arg(op, "values")]
            touch(recs[r - 1])
            changes.append(f"row {r} replaced")
        elif kind in ("insert_rows", "append_rows"):
            rows = op_arg(op, "rows", "values")
            require(isinstance(rows, list) and all(isinstance(x, list) for x in rows), "rows must be a list of lists")
            at = len(recs) + 1 if kind == "append_rows" else int(str(op_arg(op, "at", "row")).lstrip("rR"))
            require(1 <= at <= len(recs) + 1, f"'at' must be 1..{len(recs) + 1}")
            new = [_Rec([_safe(v) for v in row], None) for row in rows]
            for rec in new:
                snap.touch(rec)
            recs[at - 1:at - 1] = new
            changes.append(f"{len(new)} rows inserted at row {at}")
        elif kind == "delete_rows":
            at = int(str(op_arg(op, "at", "row")).lstrip("rR"))
            n = int(op.get("count") or 1)
            require(1 <= at and at + n - 1 <= len(recs), "rows to delete are out of range")
            for rec in recs[at - 1:at - 1 + n]:
                snap.touch(rec)
            del recs[at - 1:at - 1 + n]
            changes.append(f"deleted rows {at}..{at + n - 1}")
        elif kind == "add_column":
            name = str(op_arg(op, "name", "header"))
            values = op.get("values") or []
            for i, rec in enumerate(recs):
                rec.fields.append(_safe(name if i == 0 else (values[i - 1] if i - 1 < len(values) else "")))
                touch(rec)
            changes.append(f"added column '{name}'")
        elif kind == "replace_text":
            find, repl = str(op_arg(op, "find")), str(op.get("replace", ""))
            n = 0
            for rec in recs:
                if any(find in f for f in rec.fields):
                    rec.fields = [_safe(f.replace(find, repl)) if find in f else f for f in rec.fields]
                    touch(rec)
                    n += 1
            require(n > 0, f"'{find}' not found")
            changes.append(f"replaced '{find}' in {n} rows")
        else:
            raise DocOpError(f"unknown csv op '{op.get('op')}' (have: set_cell, set_row, insert_rows, "
                             "append_rows, delete_rows, add_column, replace_text)")

    problems = snap.problems((f"r{i}", r) for i, r in enumerate(recs, 1))
    require(not problems, "edit would change rows you did not ask for: " + "; ".join(problems[:8]))
    out = io.StringIO()
    w = csv.writer(out, dialect, lineterminator=nl)
    for i, rec in enumerate(recs):
        if rec.raw is not None:
            raw = rec.raw
            if i < len(recs) - 1 and not raw.endswith(("\n", "\r")):
                raw += nl
            out.write(raw)
        else:
            w.writerow(rec.fields)
    text = out.getvalue()
    if not trailing and text.endswith(nl) and recs and recs[-1].raw is not None:
        text = text[: -len(nl)]
    return EditResult(text.encode(enc), changes, [])


def create(rows: list[list]) -> bytes:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    for row in rows:
        w.writerow([_safe(v) for v in row])
    return out.getvalue().encode("utf-8")
