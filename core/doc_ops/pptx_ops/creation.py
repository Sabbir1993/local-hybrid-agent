import sys
from typing import Optional
from .constants import (
    R_NS,
    _open,
    _save,
)
from .text_ops import _set_frame_text
from . import design, outline
from .outline import parse_markdown_slides  # noqa: F401  (kept importable from here)


def _flat(s: dict, with_table: bool = True) -> list:
    """Bullets of a slide as plain lines (cards/stats fold into the template's body placeholder; a table too, unless it
    is drawn as a real table)."""
    lines = [b for b in s["bullets"]]
    if with_table:
        for row in s["table"]:
            lines.append(" | ".join(row))
    return lines


def _box_of(body, prs) -> tuple:
    """(x, y, w, h) in inches of the template's body area (its body placeholder, else a sensible default)."""
    if body is not None and body.width and body.height:
        return body.left / 914400, body.top / 914400, body.width / 914400, body.height / 914400
    sw, sh = prs.slide_width / 914400, prs.slide_height / 914400
    return 0.6, 1.8, sw - 1.2, sh - 2.6


def _template_table(slide, rows: list, box: tuple) -> None:
    """A real table in the template's body area (font and row heights measured, so it stays inside the box)."""
    from pptx.util import Inches, Pt
    cols = max(len(r) for r in rows)
    rows = [r + [""] * (cols - len(r)) for r in rows]
    x, y, w, h = box
    widths = design._table_widths(rows, w)
    size, heights = 11, design._table_heights(rows, widths, 11, "Calibri")
    for sz in (18, 16, 14, 12, 11):
        hs = design._table_heights(rows, widths, sz, "Calibri")
        if sum(hs) <= h:
            size, heights = sz, hs
            break
    gf = slide.shapes.add_table(len(rows), cols, Inches(x), Inches(y), Inches(w), Inches(sum(heights)))
    gf.name = "Table"
    tbl = gf.table
    for c, cw in enumerate(widths):
        tbl.columns[c].width = Inches(cw)
    for r, rh in enumerate(heights):
        tbl.rows[r].height = Inches(rh)
    for r, row in enumerate(rows):
        for c, val in enumerate(row):
            cell = tbl.cell(r, c)
            cell.text_frame.text = val
            for p in cell.text_frame.paragraphs:
                for run in p.runs:
                    run.font.size = Pt(size)
                    run.font.bold = r == 0


def _template_picture(slide, s: dict, box: tuple, body) -> None:
    """Picture on the right 40% of the body area; the body placeholder keeps the left part."""
    from pptx.util import Inches, Pt
    x, y, w, h = box
    pw = w * 0.4
    if body is not None:
        body.left, body.width = int(body.left), int(Inches(w - pw - 0.3))
    blob, dx, dy, dw, dh = design._fit_image(s["image_data"], pw, h, (s.get("image") or {}).get("fit", "cover"))
    import io
    pic = slide.shapes.add_picture(io.BytesIO(blob), Inches(x + w - pw + dx), Inches(y + dy), Inches(dw), Inches(dh))
    pic.name = "Image"
    alt = (s.get("image") or {}).get("caption") or (s.get("image") or {}).get("query") or ""
    if alt:
        pic._element.nvPicPr.cNvPr.set("descr", alt[:200])
    credit = " · ".join(b for b in ((s.get("image") or {}).get("caption"), s.get("image_credit")) if b)
    if credit:
        tb = slide.shapes.add_textbox(Inches(x + w - pw), Inches(y + h + 0.02), Inches(pw), Inches(0.3))
        tb.name = "deco-credit"
        tb.text_frame.text = credit
        for p in tb.text_frame.paragraphs:
            for run in p.runs:
                run.font.size = Pt(9)
                run.font.italic = True


def _create_from_template(deck: dict, template: bytes) -> bytes:
    """Fill a user-supplied template: its theme and layouts are kept, only text is set."""
    prs = _open(template)
    lst = prs.slides._sldIdLst
    for el in list(lst):
        prs.part.drop_rel(el.get(f"{{{R_NS}}}id"))
        lst.remove(el)
    layouts = list(prs.slide_layouts)

    def pick(title_only: bool):
        for l in layouts:
            kinds = {str(p.placeholder_format.type).split(".")[-1].split(" ")[0] for p in l.placeholders}
            if title_only and {"CENTER_TITLE", "SUBTITLE"} & kinds:
                return l
            if not title_only and "TITLE" in kinds and ({"BODY", "OBJECT"} & kinds):
                return l
        return layouts[1 if len(layouts) > 1 else 0]

    for s in deck["slides"]:
        cover = s["kind"] in ("cover", "closing", "section")
        slide = prs.slides.add_slide(pick(cover))
        if slide.shapes.title is not None:
            slide.shapes.title.text_frame.text = s["title"]
        body = next((p for p in slide.placeholders if p.placeholder_format.idx != 0), None)
        real_table = bool(s["table"]) and not cover
        lines = [s["subtitle"]] if cover and s["subtitle"] else _flat(s, with_table=not real_table)
        lines = [l for l in lines if l]
        box = _box_of(body, prs)
        has_pic = bool(s.get("image_data")) and s.get("kind") == "bullets" and not real_table
        if real_table:
            if body is not None:                        # the table takes the body area; the bullets (if any) stay above it
                if lines:
                    _set_frame_text(body.text_frame._txBody, "\n".join(l.rstrip(":") for l in lines))
                    body.height = int(body.height * 0.3)
                    box = (box[0], box[1] + box[3] * 0.34, box[2], box[3] * 0.66)
                else:
                    body._element.getparent().remove(body._element)
                    body = None
            try:
                _template_table(slide, s["table"], box)
            except Exception as e:
                print(f"[deck] template table failed ({type(e).__name__}: {e}); the rows were left out", file=sys.stderr)
        elif body is not None:
            if lines:
                _set_frame_text(body.text_frame._txBody, "\n".join(l.rstrip(":") for l in lines))
            else:
                body._element.getparent().remove(body._element)
                body = None
        if has_pic:
            try:
                _template_picture(slide, s, box, body)
            except Exception as e:
                print(f"[deck] template picture failed ({type(e).__name__}: {e}); the slide stays text-only", file=sys.stderr)
        if s["notes"]:
            slide.notes_slide.notes_text_frame.text = s["notes"]
    return _save(prs)


def attach_images(deck: dict, images: Optional[dict]) -> None:
    """images: {slide index: {data, credit_host, credit_url}} (resolved by doc_tools/slide_images.py) -> onto the slides
    that can show a picture. A web picture also gets its source page in the speaker notes."""
    for idx, info in (images or {}).items():
        if not isinstance(idx, int) or not 0 <= idx < len(deck["slides"]) or not info.get("data"):
            continue
        s = deck["slides"][idx]
        if s.get("kind") not in outline.IMAGE_KINDS or not s.get("image"):
            continue
        s["image_data"] = info["data"]
        s["image_credit"] = info.get("credit_host") or ""
        if s.get("kind") == "gallery" and info.get("extra"):
            s["image_extra_data"] = [{"data": e["data"], "credit_host": e.get("credit_host") or ""}
                                     for e in info["extra"] if e.get("data")]
            for e in info["extra"]:
                if e.get("credit_url"):
                    s["notes"] = (s["notes"] + f" Image source: {e['credit_url']}").strip()
        if info.get("credit_url"):
            s["notes"] = (s["notes"] + f" Image source: {info['credit_url']}").strip()


def create(md: str, template: Optional[bytes] = None, title: str = "", images: Optional[dict] = None) -> bytes:
    """Markdown / loose outline -> .pptx. Max outline.MAX_DECK_SLIDES slides; designed theme unless a template is given."""
    deck = outline.parse_outline(md, title)
    attach_images(deck, images)
    if template:
        return _create_from_template(deck, template)
    if deck.get("mode") == "designer":                     # picture slides drawn as HTML; None -> fall back to editable
        from . import designer
        data = designer.build(deck)
        if data:
            return data
        print("[deck] Designer style is not available here; built the editable deck instead", file=sys.stderr)
    return _save(design.build(deck))
