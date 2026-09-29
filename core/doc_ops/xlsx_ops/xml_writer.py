from lxml import etree
from .coercion import _coerce
from .constants import (
    XML_SPACE,
    _range_bounds,
    _s,
    num_to_col,
    split_ref,
)


def _row_el(sheet_data, r: int, create: bool = True):
    prev = None
    for row in sheet_data.iterfind(_s("row")):
        rn = int(row.get("r", "0"))
        if rn == r:
            return row
        if rn > r:
            break
        prev = row
    if not create:
        return None
    row = etree.Element(_s("row"), r=str(r))
    if prev is None:
        sheet_data.insert(0, row)
    else:
        prev.addnext(row)
    return row


def _cell_el(row, r: int, c: int, create: bool = True):
    ref = f"{num_to_col(c)}{r}"
    prev = None
    for cel in row.iterfind(_s("c")):
        cr = cel.get("r")
        cc = split_ref(cr)[1] if cr else 0
        if cc == c:
            return cel, False
        if cc > c:
            break
        prev = cel
    if not create:
        return None, False
    cel = etree.Element(_s("c"), r=ref)
    if prev is None:
        row.insert(0, cel)
    else:
        prev.addnext(cel)
    return cel, True


def _grow_dimension(root, r: int, c: int):
    dim = root.find(_s("dimension"))
    if dim is None:
        return
    r1, c1, r2, c2 = _range_bounds(dim.get("ref"))
    if r > r2 or c > c2 or r < r1 or c < c1:
        r1, c1, r2, c2 = min(r1, r), min(c1, c), max(r2, r), max(c2, c)
        dim.set("ref", f"{num_to_col(c1)}{r1}:{num_to_col(c2)}{r2}" if (r1, c1) != (r2, c2) else f"{num_to_col(c1)}{r1}")


def _write(ctx, part: str, root, r: int, c: int, value, allow_formula: bool = False,
           style_from_above: bool = True) -> None:
    kind, payload = _coerce(value, allow_formula)
    sd = root.find(_s("sheetData"))
    row = _row_el(sd, r)
    cel_existing, _ = _cell_el(row, r, c, create=False)
    ctx.check_writable(part, r, c, cel_existing)
    cel, created = _cell_el(row, r, c)
    ctx.snap.touch(cel)
    ctx.snap.touch(row)
    if created and style_from_above and r > 2:
        above = _row_el(sd, r - 1, create=False)
        if above is not None:
            a_cel, _ = _cell_el(above, r - 1, c, create=False)
            if a_cel is not None and a_cel.get("s"):
                cel.set("s", a_cel.get("s"))
    if cel.find(_s("f")) is not None:
        ctx.formula_changed = True
    for ch in list(cel):
        if etree.QName(ch).localname in ("f", "v", "is"):
            cel.remove(ch)
    cel.attrib.pop("t", None)
    if kind == "formula":
        etree.SubElement(cel, _s("f")).text = payload
        ctx.formula_changed = True
    elif kind == "bool":
        cel.set("t", "b")
        etree.SubElement(cel, _s("v")).text = "1" if payload else "0"
    elif kind == "num":
        etree.SubElement(cel, _s("v")).text = repr(payload) if isinstance(payload, float) else str(payload)
    elif kind == "str":
        cel.set("t", "inlineStr")
        is_ = etree.SubElement(cel, _s("is"))
        t = etree.SubElement(is_, _s("t"))
        t.text = payload
        if payload != payload.strip():
            t.set(XML_SPACE, "preserve")
    row.attrib.pop("spans", None)
    _grow_dimension(root, r, c)
    ctx.cells_changed = True
    ctx.book.dirty.add(part)
    ctx.rules.changed.add(part)
