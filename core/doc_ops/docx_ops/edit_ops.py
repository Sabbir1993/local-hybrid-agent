import copy
import io
from lxml import etree
from ..base import (
    EditResult,
    PartRules,
    Snapshot,
    check_package,
    op_arg,
    require,
    xml_fp,
)
from .constants import (
    _ADDR,
    _cells,
    _open,
    _paras,
    _rows,
    _tables,
    _w,
)
from .text_ops import (
    _set_cell_text,
    _style_id,
    replace_in_para,
    set_para_text,
)


class _Ctx:
    def __init__(self, doc):
        self.doc = doc
        self.snap = Snapshot(xml_fp)
        self.snap.take(self.items())
        self.rules = PartRules()
        self.changes: list[str] = []

    def items(self):
        out = []
        for i, el in enumerate(self.doc.element.body, 1):
            out.append((f"body[{i}] {etree.QName(el).localname}", el))
        return out

    def resolve(self, addr: str):
        m = _ADDR.match(str(addr).strip().replace(" ", ""))
        require(bool(m), f"bad address '{addr}' (use p12, t2, t2[3,1], hdr1/p1)")
        hf, sec, pn, tn, r, c = m.groups()
        if hf:
            si = int(sec)
            require(1 <= si <= len(self.doc.sections), f"section {si} does not exist")
            part = self.doc.sections[si - 1].header if hf.lower() == "hdr" else self.doc.sections[si - 1].footer
            require(not part.is_linked_to_previous, f"section {si} has no own {'header' if hf.lower() == 'hdr' else 'footer'}")
            self.rules.changed.add(part.part.partname.lstrip("/"))
            container = part._element
            require(pn is not None, "headers/footers are addressed by paragraph (hdr1/p2)")
            ps = container.findall(_w("p"))
            require(1 <= int(pn) <= len(ps), f"{hf}{si} has {len(ps)} paragraphs")
            return "p", ps[int(pn) - 1]
        self.rules.changed.add("word/document.xml")
        if pn:
            ps = _paras(self.doc)
            require(1 <= int(pn) <= len(ps), f"document has {len(ps)} paragraphs, not p{pn}")
            return "p", ps[int(pn) - 1]
        ts = _tables(self.doc)
        require(1 <= int(tn) <= len(ts), f"document has {len(ts)} tables, not t{tn}")
        tbl = ts[int(tn) - 1]
        if r:
            rows = _rows(tbl)
            require(1 <= int(r) <= len(rows), f"t{tn} has {len(rows)} rows")
            cells = _cells(rows[int(r) - 1])
            require(1 <= int(c) <= len(cells), f"row {r} of t{tn} has {len(cells)} cells")
            return "cell", cells[int(c) - 1]
        return "tbl", tbl


def _op_set_text(ctx: _Ctx, op: dict):
    addr = op_arg(op, "addr", "target", "address")
    text = str(op_arg(op, "text", "value", default=""))
    kind, el = ctx.resolve(addr)
    require(kind in ("p", "cell"), f"'{addr}' is a table; address a cell like {addr}[1,1]")
    ctx.snap.touch(el)
    (set_para_text if kind == "p" else _set_cell_text)(el, text)
    ctx.changes.append(f"{addr}: text set")


def _op_insert_paragraph(ctx: _Ctx, op: dict):
    addr = op_arg(op, "after", "addr", "target")
    kind, el = ctx.resolve(addr)
    require(kind in ("p", "tbl"), "insert_paragraph_after needs a paragraph or table address")
    texts = op.get("texts") or [op_arg(op, "text", default="")]
    style = op.get("style")
    like = el if kind == "p" else None
    anchor = el
    for t in texts:
        if like is not None:
            new = copy.deepcopy(like)
        else:
            new = etree.Element(_w("p"))
        anchor.addnext(new)
        set_para_text(new, str(t))
        if style:
            sid = _style_id(ctx.doc, style)
            ppr = new.find(_w("pPr"))
            if ppr is None:
                ppr = etree.Element(_w("pPr"))
                new.insert(0, ppr)
            ps = ppr.find(_w("pStyle"))
            if ps is None:
                ps = etree.Element(_w("pStyle"))
                ppr.insert(0, ps)
            ps.set(_w("val"), sid)
        ctx.snap.touch(new)
        anchor = new
    ctx.changes.append(f"inserted {len(texts)} paragraph(s) after {addr}")


def _op_delete(ctx: _Ctx, op: dict):
    addr = op_arg(op, "addr", "target")
    kind, el = ctx.resolve(addr)
    require(kind in ("p", "tbl"), "delete needs a paragraph or table address")
    ctx.snap.touch(el)
    el.getparent().remove(el)
    ctx.changes.append(f"deleted {addr}")


def _op_add_table_row(ctx: _Ctx, op: dict):
    addr = op_arg(op, "addr", "table", "target")
    kind, tbl = ctx.resolve(addr)
    require(kind == "tbl", "add_table_row needs a table address like t2")
    rows = _rows(tbl)
    after = int(op.get("after") or len(rows))
    require(1 <= after <= len(rows), f"'after' must be 1..{len(rows)}")
    values = op_arg(op, "values", "cells")
    src = rows[after - 1]
    new = copy.deepcopy(src)
    src.addnext(new)
    for i, tc in enumerate(_cells(new)):
        _set_cell_text(tc, str(values[i]) if i < len(values) else "")
    ctx.snap.touch(new)
    ctx.changes.append(f"{addr}: row added after row {after}")


def _op_replace_text(ctx: _Ctx, op: dict):
    find = str(op_arg(op, "find", "old"))
    repl = str(op.get("replace", op.get("new", "")))
    require(find != "", "replace_text needs a non-empty 'find'")
    hits = 0
    containers = [("word/document.xml", ctx.doc.element.body)]
    for sec in ctx.doc.sections:
        for hf in (sec.header, sec.footer):
            if not hf.is_linked_to_previous:
                containers.append((hf.part.partname.lstrip("/"), hf._element))
    for part, root in containers:
        for p in list(root.iter(_w("p"))):
            if replace_in_para(p, find, repl):
                ctx.snap.touch(p)
                ctx.rules.changed.add(part)
                hits += 1
    require(hits > 0, f"'{find}' not found in the document")
    ctx.changes.append(f"replaced '{find}' -> '{repl}' in {hits} paragraph(s)")


OPS = {
    "set_text": _op_set_text,
    "set_paragraph": _op_set_text,
    "set_table_cell": _op_set_text,
    "insert_paragraph_after": _op_insert_paragraph,
    "delete_paragraph": _op_delete,
    "delete": _op_delete,
    "add_table_row": _op_add_table_row,
    "replace_text": _op_replace_text,
}


def apply(data: bytes, ops: list[dict]) -> EditResult:
    doc = _open(data)
    ctx = _Ctx(doc)
    for op in ops:
        fn = OPS.get(str(op.get("op", "")).lower())
        require(fn is not None, f"unknown docx op '{op.get('op')}' (have: {', '.join(OPS)})")
        fn(ctx, op)
    problems = ctx.snap.problems(ctx.items())
    require(not problems, "edit would change parts you did not ask for: " + "; ".join(problems[:8]))
    buf = io.BytesIO()
    doc.save(buf)
    out = buf.getvalue()
    problems = check_package(data, out, ctx.rules)
    require(not problems, "edit would change untouched document parts: " + "; ".join(problems[:8]))
    return EditResult(out, ctx.changes, [])
