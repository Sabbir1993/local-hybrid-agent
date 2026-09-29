from lxml import etree
from ..base import DocOpError, op_arg, require
from .constants import (
    RT_CALC,
    _fmt,
    _range_bounds,
    _s,
    _split_addr,
    num_to_col,
    split_ref,
)
from .xml_context import _Ctx, _sheet_items
from .xml_writer import _cell_el, _row_el, _write


def _op_set_cell(ctx: _Ctx, op: dict, formula: bool = False):
    sheet, ref = _split_addr(op_arg(op, "addr", "cell", "target"))
    value = op.get("formula") if formula and "formula" in op else op.get("value", op.get("text"))
    if formula and value is not None and not str(value).startswith("="):
        value = "=" + str(value)
    name, part, root = ctx.sheet(sheet)
    r, c = split_ref(ref)
    _write(ctx, part, root, r, c, value, allow_formula=formula)
    ctx.changes.append(f"{name}!{ref} = {_fmt(value)}")


def _op_set_range(ctx: _Ctx, op: dict):
    sheet, ref = _split_addr(op_arg(op, "addr", "range", "target"))
    values = op_arg(op, "values", "rows")
    require(isinstance(values, list) and all(isinstance(r, list) for r in values),
            "set_range needs values as a list of rows, e.g. [[1, 2], [3, 4]]")
    name, part, root = ctx.sheet(sheet)
    r0, c0 = split_ref(ref.split(":")[0])
    if ":" in ref:
        r1, c1, r2, c2 = _range_bounds(ref)
        require(len(values) <= r2 - r1 + 1 and all(len(v) <= c2 - c1 + 1 for v in values),
                f"values do not fit {ref}")
    n = 0
    for i, rowvals in enumerate(values):
        for j, v in enumerate(rowvals):
            _write(ctx, part, root, r0 + i, c0 + j, v, allow_formula=bool(op.get("formulas")))
            n += 1
    ctx.changes.append(f"{name}!{ref}: {n} cells set")


def _op_clear(ctx: _Ctx, op: dict):
    sheet, ref = _split_addr(op_arg(op, "addr", "range", "target"))
    name, part, root = ctx.sheet(sheet)
    r1, c1, r2, c2 = _range_bounds(ref)
    require((r2 - r1 + 1) * (c2 - c1 + 1) <= 100_000, "range too large to clear")
    sd = root.find(_s("sheetData"))
    for r in range(r1, r2 + 1):
        row = _row_el(sd, r, create=False)
        if row is None:
            continue
        for c in range(c1, c2 + 1):
            cel, _ = _cell_el(row, r, c, create=False)
            if cel is not None:
                _write(ctx, part, root, r, c, None)
    ctx.changes.append(f"{name}!{ref} cleared (formatting kept)")


def _op_append_rows(ctx: _Ctx, op: dict):
    sheet = op.get("sheet") or _split_addr(op.get("addr") or "")[0]
    rows = op_arg(op, "rows", "values")
    require(isinstance(rows, list) and all(isinstance(r, list) for r in rows),
            "append_rows needs rows as a list of lists")
    name, part, root = ctx.sheet(sheet)
    sd = root.find(_s("sheetData"))
    last = 0
    first_col = 1
    for row in sd.iterfind(_s("row")):
        if any(c.find(_s("v")) is not None or c.find(_s("is")) is not None or c.find(_s("f")) is not None
               for c in row.iterfind(_s("c"))):
            last = max(last, int(row.get("r", "0")))
    last_row = _row_el(sd, last, create=False) if last else None
    if last_row is not None:
        cols = [split_ref(c.get("r"))[1] for c in last_row.iterfind(_s("c")) if c.get("r")]
        first_col = min(cols) if cols else 1
    for i, vals in enumerate(rows):
        r = last + 1 + i
        for j, v in enumerate(vals):
            c = first_col + j
            _write(ctx, part, root, r, c, v, allow_formula=bool(op.get("formulas")), style_from_above=False)
            if last_row is not None:
                src, _ = _cell_el(last_row, last, c, create=False)
                cel, _ = _cell_el(_row_el(sd, r), r, c, create=False)
                if src is not None and src.get("s") and cel is not None:
                    cel.set("s", src.get("s"))
    for tp, t in ctx._tables[part]:
        r1, c1, r2, c2 = _range_bounds(t.get("ref"))
        if r2 == last:
            new_ref = f"{num_to_col(c1)}{r1}:{num_to_col(c2)}{last + len(rows)}"
            t.set("ref", new_ref)
            af = t.find(_s("autoFilter"))
            if af is not None:
                af.set("ref", new_ref)
            ctx.book.dirty.add(tp)
            ctx.rules.changed.add(tp)
            ctx.notes.append(f"table '{t.get('displayName')}' extended to {new_ref}")
    ctx.changes.append(f"{name}: appended {len(rows)} rows after row {last}")


XML_OPS = {
    "set_cell": _op_set_cell,
    "set_formula": lambda ctx, op: _op_set_cell(ctx, op, formula=True),
    "set_range": _op_set_range,
    "clear": _op_clear,
    "clear_range": _op_clear,
    "append_rows": _op_append_rows,
}


def _finish_xml(ctx: _Ctx) -> bytes:
    book = ctx.book
    if ctx.formula_changed:
        calc = [r for r in book.wb_rels if r.get("Type") == RT_CALC]
        for r in calc:
            book.wb_rels.remove(r)
            book.removed.add("xl/calcChain.xml")
        if calc:
            book.trees["xl/_rels/workbook.xml.rels"] = book.wb_rels
            book.dirty.add("xl/_rels/workbook.xml.rels")
            ct = book.tree("[Content_Types].xml")
            for o in list(ct):
                if o.get("PartName", "").lstrip("/") == "xl/calcChain.xml":
                    ct.remove(o)
            book.dirty.add("[Content_Types].xml")
            ctx.rules.changed |= {"xl/workbook.xml:rels", "[Content_Types].xml"}
            ctx.rules.removed.add("xl/calcChain.xml")
    if ctx.cells_changed and (ctx.formula_changed or book.has_formulas()):
        calc_pr = book.wb.find(_s("calcPr"))
        if calc_pr is None:
            calc_pr = etree.SubElement(book.wb, _s("calcPr"))
            for tag in ("oleSize", "customWorkbookViews", "pivotCaches", "smartTagPr", "smartTagTypes",
                        "webPublishing", "fileRecoveryPr", "webPublishObjects", "extLst"):
                nxt = book.wb.find(_s(tag))
                if nxt is not None:
                    nxt.addprevious(calc_pr)
                    break
        calc_pr.set("fullCalcOnLoad", "1")
        book.trees["xl/workbook.xml"] = book.wb
        book.dirty.add("xl/workbook.xml")
        ctx.rules.changed.add("xl/workbook.xml")
        ctx.notes.append("formulas will recalculate when the file is opened")
    items = []
    for n, p in book.sheets:
        if p in ctx._snapped:
            items += _sheet_items(n, book.trees[p])
    problems = ctx.snap.problems(items)
    if problems:
        raise DocOpError("edit would change cells you did not ask for: " + "; ".join(problems[:8]))
    return book.save()
