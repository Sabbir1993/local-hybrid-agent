import io
from typing import Optional
from ..base import DocOpError, EditResult, check_package, op_arg, require
from .constants import (
    R_NS,
    _a,
    _body_shape,
    _find_shape,
    _iter_shapes,
    _open,
    _parse,
    _save,
    _slide_at,
    _title_shape,
)
from .edit_context import (
    _Ctx,
    _duplicate,
    _move_sldId,
    _rename_parts,
    _sldId_of,
    _snapshot_items,
)
from .text_ops import (
    _set_frame_text,
    _set_para_text,
    _txbody_of,
    replace_in_paragraph,
)


def _target_txbody(ctx: _Ctx, addr: str):
    si, part, shid, pi, cell = _parse(addr)
    slide = _slide_at(ctx.prs, si)
    if part == "notes":
        ns = slide.notes_slide
        k = slide.slide_id
        ctx.rules.changed |= {
            f"notes:{k}", f"notes:{k}:rels", f"slide:{k}:rels", "[Content_Types].xml",
            "ppt/presentation.xml", "ppt/presentation.xml:rels",
        }
        ctx.rules.added_prefixes |= {"ppt/notesMasters/", "ppt/theme/", f"notes:{k}", f"notes:{k}:rels"}
        ctx.snap.touch(ns._element)
        for el in ns._element.iter():
            ctx.snap.touch(el, ancestors=False)
        return slide, ns.notes_text_frame._txBody, None
    sh = _title_shape(slide) if part == "title" else (_find_shape(slide, shid) if shid else None)
    require(sh is not None, f"address '{addr}' must name a shape (s3/sh5), title or notes")
    if cell:
        require(getattr(sh, "has_table", False) and sh.has_table, f"sh{sh.shape_id} is not a table")
        r, c = cell
        tbl = sh.table
        require(1 <= r <= len(tbl.rows) and 1 <= c <= len(tbl.columns), f"cell [{r},{c}] outside the table")
        return slide, tbl.cell(r - 1, c - 1).text_frame._txBody, None
    body = _txbody_of(sh)
    if pi:
        paras = body.findall(_a("p"))
        require(1 <= pi <= len(paras), f"sh{sh.shape_id} has {len(paras)} paragraphs, not p{pi}")
        return slide, body, paras[pi - 1]
    return slide, body, None


def _op_set_text(ctx: _Ctx, op: dict):
    addr = op_arg(op, "addr", "target", "address")
    text = str(op_arg(op, "text", "value", default=""))
    slide, body, para = _target_txbody(ctx, addr)
    ctx.text_edit(slide, body)
    if para is not None:
        _set_para_text(para, text)
    else:
        _set_frame_text(body, text)
    ctx.changes.append(f"{addr}: text set")


def _all_txbodies(ctx: _Ctx, scope: Optional[str]):
    slides = list(enumerate(ctx.prs.slides, 1))
    if scope:
        si = _parse(scope)[0]
        slides = [(si, _slide_at(ctx.prs, si))]
    for i, slide in slides:
        for sh in _iter_shapes(slide.shapes):
            if sh.has_text_frame:
                yield i, slide, f"s{i}/sh{sh.shape_id}", sh.text_frame._txBody
            elif getattr(sh, "has_table", False) and sh.has_table:
                for r, row in enumerate(sh.table.rows, 1):
                    for c, cell in enumerate(row.cells, 1):
                        yield i, slide, f"s{i}/sh{sh.shape_id}/tbl[{r},{c}]", cell.text_frame._txBody


def _op_replace_text(ctx: _Ctx, op: dict):
    find = str(op_arg(op, "find", "old", "search"))
    repl = str(op_arg(op, "replace", "new", "text", default=""))
    require(find != "", "replace_text needs a non-empty 'find'")
    hits = []
    for _i, slide, label, body in _all_txbodies(ctx, op.get("scope")):
        for p in body.findall(_a("p")):
            if replace_in_paragraph(p, find, repl):
                ctx.text_edit(slide, p)
                hits.append(label)
    require(bool(hits), f"'{find}' not found in the presentation")
    ctx.changes.append(f"replaced '{find}' -> '{repl}' in {', '.join(dict.fromkeys(hits))}")


def _op_duplicate_slide(ctx: _Ctx, op: dict):
    si = _parse(op_arg(op, "addr", "slide", "target"))[0]
    src = _slide_at(ctx.prs, si)
    ctx.structural()
    new = _duplicate(ctx, src)
    after = int(op.get("after") or si)
    _move_sldId(ctx.prs, _sldId_of(ctx.prs, new), after)
    ctx.changes.append(f"duplicated s{si} as new slide {after + 1}")
    return new


def _op_add_slide(ctx: _Ctx, op: dict):
    prs = ctx.prs
    n = len(prs.slides)
    after = op.get("after")
    after = n if after in (None, "", "end") else _parse(after)[0] if str(after).lower().startswith("s") else int(after)
    require(0 <= after <= n, f"'after' must be 0..{n}")
    title = str(op.get("title") or "")
    bullets = op.get("bullets") or op.get("body") or []
    if isinstance(bullets, str):
        bullets = [b for b in bullets.split("\n")]
    ctx.structural()
    layout_name = op.get("layout")
    like = op.get("like")
    if layout_name:
        layouts = {l.name.lower(): l for m in prs.slide_masters for l in m.slide_layouts}
        lay = layouts.get(str(layout_name).lower())
        require(lay is not None, f"layout '{layout_name}' not found (have: {', '.join(layouts)})")
        new = prs.slides.add_slide(lay)
        ctx.new_slide(new)
    else:
        if like:
            src = _slide_at(prs, _parse(like)[0])
        else:
            pool = [prs.slides[i] for i in range(max(after - 1, 0), n)] + list(prs.slides)
            src = next((s for s in pool if s.shapes.title is not None or
                        sum(1 for x in s.shapes if x.has_text_frame) >= 2), prs.slides[max(after - 1, 0)] if n else None)
        if src is None:
            new = prs.slides.add_slide(prs.slide_layouts[1 if len(prs.slide_layouts) > 1 else 0])
            ctx.new_slide(new)
        else:
            new = _duplicate(ctx, src)
    tshape = _title_shape(new)
    if tshape is not None:
        _set_frame_text(tshape.text_frame._txBody, title)
        body = _body_shape(new, tshape)
        if body is not None:
            _set_frame_text(body.text_frame._txBody, "\n".join(str(b) for b in bullets) if bullets else "")
        for s in new.shapes:
            if s.has_text_frame and s.shape_id not in (tshape.shape_id, body.shape_id if body else -1) \
                    and not s.is_placeholder:
                _set_frame_text(s.text_frame._txBody, "")
    _move_sldId(prs, _sldId_of(prs, new), after)
    if op.get("notes"):
        new.notes_slide.notes_text_frame.text = str(op["notes"])
        k = new.slide_id
        ctx.rules.added_prefixes |= {f"notes:{k}", f"notes:{k}:rels", "ppt/notesMasters/", "ppt/theme/"}
    ctx.changes.append(f"added slide {after + 1} '{title}'")


def _op_delete_slide(ctx: _Ctx, op: dict):
    prs = ctx.prs
    si = _parse(op_arg(op, "addr", "slide", "target"))[0]
    slide = _slide_at(prs, si)
    require(len(prs.slides) > 1, "cannot delete the only slide")
    ctx.structural()
    k = slide.slide_id
    ctx.rules.removed |= {f"slide:{k}", f"slide:{k}:rels", f"notes:{k}", f"notes:{k}:rels"}
    el = _sldId_of(prs, slide)
    for x in slide._element.iter():
        ctx.snap.touch(x, ancestors=False)
    prs.part.drop_rel(el.get(f"{{{R_NS}}}id"))
    prs.slides._sldIdLst.remove(el)
    ctx.changes.append(f"deleted slide {si}")


def _op_move_slide(ctx: _Ctx, op: dict):
    prs = ctx.prs
    si = _parse(op_arg(op, "addr", "slide", "target"))[0]
    to = int(op_arg(op, "to", "position"))
    require(1 <= to <= len(prs.slides), f"'to' must be 1..{len(prs.slides)}")
    slide = _slide_at(prs, si)
    ctx.structural()
    _move_sldId(prs, _sldId_of(prs, slide), to - 1)
    ctx.changes.append(f"moved slide {si} to position {to}")


def _op_set_notes(ctx: _Ctx, op: dict):
    si = _parse(op_arg(op, "addr", "slide", "target"))[0]
    op = dict(op, addr=f"s{si}/notes")
    _op_set_text(ctx, op)


def _op_replace_image(ctx: _Ctx, op: dict):
    addr = op_arg(op, "addr", "target")
    si, _part, shid, _pi, _cell = _parse(addr)
    slide = _slide_at(ctx.prs, si)
    require(shid is not None, "replace_image needs a picture address like s2/sh4")
    sh = _find_shape(slide, shid)
    blip = sh._element.find(".//" + _a("blip"))
    require(blip is not None, f"sh{shid} is not a picture")
    img = op.get("_image_bytes")
    require(isinstance(img, (bytes, bytearray)) and img, "replace_image needs 'image' (a file you uploaded)")
    _img_part, rid = slide.part.get_or_add_image_part(io.BytesIO(img))
    ctx.text_edit(slide, sh._element)
    blip.set(f"{{{R_NS}}}embed", rid)
    k = slide.slide_id
    ctx.rules.changed |= {f"slide:{k}:rels", "[Content_Types].xml"}
    ctx.rules.added_prefixes.add("ppt/media/")
    ctx.changes.append(f"{addr}: image replaced")


OPS = {
    "set_text": _op_set_text,
    "set_table_cell": _op_set_text,
    "replace_text": _op_replace_text,
    "add_slide": _op_add_slide,
    "duplicate_slide": _op_duplicate_slide,
    "delete_slide": _op_delete_slide,
    "move_slide": _op_move_slide,
    "set_notes": _op_set_notes,
    "replace_image": _op_replace_image,
}


def apply(data: bytes, ops: list[dict]) -> EditResult:
    prs = _open(data)
    ctx = _Ctx(prs)
    for op in ops:
        fn = OPS.get(str(op.get("op", "")).lower())
        require(fn is not None, f"unknown pptx op '{op.get('op')}' (have: {', '.join(OPS)})")
        fn(ctx, op)
    problems = ctx.snap.problems(_snapshot_items(prs))
    if problems:
        raise DocOpError("edit would change parts you did not ask for: " + "; ".join(problems[:8]))
    out = _save(prs)
    problems = check_package(data, out, ctx.rules, _rename_parts)
    if problems:
        raise DocOpError("edit would change untouched package parts: " + "; ".join(problems[:8]))
    return EditResult(out, ctx.changes, ctx.notes)
