"""Renders a parsed deck (outline.parse_outline) as a designed 16:9 presentation with python-pptx.

Titles and bullet bodies stay real placeholders (so doc_inspect/doc_edit address them like any deck);
everything decorative is a shape named "deco*" that inspection skips.
"""
import io
import math
from lxml import etree
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Inches, Pt
from . import outline as O
from .text_ops import _set_frame_text

W, H = 13.333, 7.5
MX = 0.7                      # side margin
DECO = "deco"


def _rgb(h: str) -> RGBColor:
    return RGBColor.from_string(h.upper())


def _lum(h: str) -> float:
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _on(h: str) -> str:
    """Readable text colour on top of fill h."""
    return "111111" if _lum(h) > 0.55 else "FFFFFF"


def _fit(texts: list, width: float, height: float, base: float, floor: float, gap_pt: float = 6.0) -> float:
    size = base
    while size > floor:
        cpl = max(1, int(width * 72 / (size * 0.54)))
        lines = sum(max(1, math.ceil(len(t) / cpl)) for t in texts)
        if lines * size * 1.22 / 72 + len(texts) * gap_pt / 72 <= height:
            break
        size -= 1
    return max(size, floor)


def _back(shape) -> None:
    el = shape._element
    tree = el.getparent()
    tree.remove(el)
    tree.insert(2, el)


def _deco(slide, kind, x, y, w, h, fill, alpha=None, back=True, name=DECO):
    sh = slide.shapes.add_shape(kind, Inches(x), Inches(y), Inches(w), Inches(h))
    sh.name = name
    sh.fill.solid()
    sh.fill.fore_color.rgb = _rgb(fill)
    sh.line.fill.background()
    sh.shadow.inherit = False
    if alpha is not None:
        clr = sh.fill._xPr.find(qn("a:solidFill"))[0]
        etree.SubElement(clr, qn("a:alpha")).set("val", str(int(alpha * 1000)))
    if back:
        _back(sh)
    return sh


def _bg(slide, hex_):
    slide.background.fill.solid()
    slide.background.fill.fore_color.rgb = _rgb(hex_)


def _style_runs(p, font, size, color, bold=False, italic=False):
    for r in p.runs:
        r.font.name = font
        r.font.size = Pt(size)
        r.font.bold = bold
        r.font.italic = italic
        r.font.color.rgb = _rgb(color)


def _frame(tf, anchor=MSO_ANCHOR.TOP, margins=(0.0, 0.0, 0.0, 0.0)):
    tf.word_wrap = True
    tf.auto_size = MSO_AUTO_SIZE.NONE
    tf.vertical_anchor = anchor
    tf.margin_left, tf.margin_top, tf.margin_right, tf.margin_bottom = (Inches(m) for m in margins)


def _place(shape, x, y, w, h):
    shape.left, shape.top, shape.width, shape.height = Inches(x), Inches(y), Inches(w), Inches(h)


def _textbox(slide, x, y, w, h, paras, name=DECO + "-text", anchor=MSO_ANCHOR.TOP, align=PP_ALIGN.LEFT):
    """paras: [(text, font, size, color, bold, italic, space_after_pt)]"""
    tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tb.name = name
    _frame(tb.text_frame, anchor)
    for i, (text, font, size, color, bold, italic, after) in enumerate(paras):
        p = tb.text_frame.paragraphs[0] if i == 0 else tb.text_frame.add_paragraph()
        p.text = text
        p.alignment = align
        p.space_after = Pt(after)
        _style_runs(p, font, size, color, bold, italic)
    return tb


def _bullet(p, color, level, glyph="•"):
    ppr = p._p.get_or_add_pPr()
    marl, ind = (Inches(0.34), -Inches(0.34)) if level == 0 else (Inches(0.78), -Inches(0.28))
    ppr.set("marL", str(int(marl)))
    ppr.set("indent", str(int(ind)))
    for tag in ("a:buClr", "a:buFont", "a:buChar", "a:buNone", "a:buAutoNum"):
        for el in ppr.findall(qn(tag)):
            ppr.remove(el)
    etree.SubElement(etree.SubElement(ppr, qn("a:buClr")), qn("a:srgbClr")).set("val", color)
    etree.SubElement(ppr, qn("a:buFont")).set("typeface", "Arial")
    etree.SubElement(ppr, qn("a:buChar")).set("char", glyph if level == 0 else "–")


def _fill_bullets(shape, items: list, t: dict, x, y, w, h, base=22, floor=13):
    _place(shape, x, y, w, h)
    _frame(shape.text_frame, MSO_ANCHOR.TOP)
    _set_frame_text(shape.text_frame._txBody, "\n".join(items))
    size = _fit([i.strip() for i in items], w - 0.4, h, base, floor, gap_pt=16)
    for p in shape.text_frame.paragraphs:
        level = p._p.pPr.get("lvl") if p._p.pPr is not None else None
        lvl = 1 if level and level != "0" else 0
        p.space_after = Pt(16 if lvl == 0 else 6)
        p.alignment = PP_ALIGN.LEFT
        _style_runs(p, t["body"], size if lvl == 0 else max(size - 3, floor), t["text"] if lvl == 0 else t["muted"])
        _bullet(p, t["accent"], lvl)


def _title(slide, text, t, x=MX, y=0.5, w=W - 2 * MX, h=1.0, color=None, base=32, bold=True,
           anchor=MSO_ANCHOR.MIDDLE):
    sh = slide.shapes.title
    sh.text_frame.text = text
    _place(sh, x, y, w, h)
    _frame(sh.text_frame, anchor)
    size = _fit([text], w, h, base, 20, gap_pt=0)
    for p in sh.text_frame.paragraphs:
        p.alignment = PP_ALIGN.LEFT
        _style_runs(p, t["head"], size, color or t["text"], bold)
    return sh


def _drop_empty_placeholders(slide):
    for ph in list(slide.placeholders):
        if ph.placeholder_format.idx != 0 and ph.has_text_frame and not ph.text_frame.text.strip():
            ph._element.getparent().remove(ph._element)


def _chrome(slide, t, title, deck_title, num):
    _bg(slide, t["bg"])
    _title(slide, title, t)
    _deco(slide, MSO_SHAPE.RECTANGLE, MX, 1.55, 0.9, 0.07, t["accent"])
    _textbox(slide, MX, 6.98, 8.5, 0.3, [(deck_title, t["body"], 10, t["muted"], False, False, 0)],
             name=DECO + "-footer", anchor=MSO_ANCHOR.MIDDLE)
    _textbox(slide, W - MX - 0.8, 6.98, 0.8, 0.3, [(str(num), t["body"], 10, t["muted"], True, False, 0)],
             name=DECO + "-number", anchor=MSO_ANCHOR.MIDDLE, align=PP_ALIGN.RIGHT)


def _fit_image(data: bytes, w: float, h: float, fit: str = "cover"):
    """(bytes, left, top, width, height) in inches: 'cover' crops the picture to the w x h box, 'contain' keeps it whole
    and centres it. Re-encoded (JPEG for photos), so the deck stays small."""
    from PIL import Image
    im = Image.open(io.BytesIO(data))
    im.load()
    has_alpha = im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)
    im = im.convert("RGBA" if has_alpha else "RGB")
    iw, ih = im.size
    box = w / h
    if fit == "cover":
        if iw / ih > box:
            nw = int(ih * box)
            im = im.crop(((iw - nw) // 2, 0, (iw - nw) // 2 + nw, ih))
        else:
            nh = int(iw / box)
            im = im.crop((0, (ih - nh) // 2, iw, (ih - nh) // 2 + nh))
        dx, dy, dw, dh = 0.0, 0.0, w, h
    else:
        scale = min(w / iw, h / ih)
        dw, dh = iw * scale, ih * scale
        dx, dy = (w - dw) / 2, (h - dh) / 2
    out = io.BytesIO()
    if has_alpha:
        im.save(out, format="PNG")
    else:
        im.save(out, format="JPEG", quality=85, optimize=True)
    return out.getvalue(), dx, dy, dw, dh


def _picture(slide, s, x, y, w, h, name="Image"):
    """Add the slide's resolved picture in the (x, y, w, h) box; returns the shape (None when there is no picture)."""
    data = s.get("image_data")
    if not data:
        return None
    spec = s.get("image") or {}
    blob, dx, dy, dw, dh = _fit_image(data, w, h, spec.get("fit", "cover"))
    pic = slide.shapes.add_picture(io.BytesIO(blob), Inches(x + dx), Inches(y + dy), Inches(dw), Inches(dh))
    pic.name = name
    alt = spec.get("caption") or spec.get("query") or ""
    if alt:
        pic._element.nvPicPr.cNvPr.set("descr", alt[:200])
    return pic


def _credit(slide, s, t, x, y, w, color=None, align=PP_ALIGN.RIGHT):
    """Small source line under / over a picture: the caption, then 'Image: host' for web pictures."""
    spec = s.get("image") or {}
    bits = [b for b in (spec.get("caption"), s.get("image_credit")) if b]
    if bits and s.get("image_data"):
        _textbox(slide, x, y, w, 0.3, [("  ·  ".join(bits), t["body"], 9, color or t["muted"], False, True, 0)],
                 name=DECO + "-credit", anchor=MSO_ANCHOR.MIDDLE, align=align)


def _heading_body(item):
    head, kids = item
    head = head.rstrip(":").strip()
    if kids:
        return head, kids
    lb = O.label_split(head)
    return (lb[0], [lb[1]]) if lb else (head, [])


# ---------------------------------------------------------------- slide kinds

def _cover(slide, s, t, closing=False):
    _bg(slide, t["cover_bg"])
    motif = t["motif"]
    tw = 10.4 if motif == "rule" else 8.0
    has_pic = bool(s.get("image_data")) and not closing
    if has_pic:
        tw = 6.6
    if motif == "circles":
        _deco(slide, MSO_SHAPE.OVAL, 8.9, -1.9, 7.2, 7.2, t["accent"], alpha=16)
        _deco(slide, MSO_SHAPE.OVAL, 10.6, 3.5, 5.2, 5.2, t["accent2"], alpha=28)
    elif motif == "band":
        _deco(slide, MSO_SHAPE.RECTANGLE, 10.2, 0, W - 10.2, H, t["accent2"], alpha=90)
        _deco(slide, MSO_SHAPE.RECTANGLE, 10.0, 0, 0.2, H, t["accent"])
    else:
        _deco(slide, MSO_SHAPE.RECTANGLE, 1.1, 1.4, 0.9, 0.1, t["accent"])
        _deco(slide, MSO_SHAPE.RECTANGLE, 1.1, 6.6, 11.1, 0.02, t["muted"], alpha=40)
    _deco(slide, MSO_SHAPE.RECTANGLE, 0, 0, 0.28, H, t["accent"])
    if has_pic:                                       # full-height picture panel on the right edge
        _picture(slide, s, 8.0, 0, W - 8.0, H, name="Cover image")
        _deco(slide, MSO_SHAPE.RECTANGLE, 7.9, 0, 0.1, H, t["accent"], back=False)
        _credit(slide, s, t, 8.1, 7.1, W - 8.3, color="FFFFFF")
    _title(slide, s["title"], t, 1.1, 2.0, tw, 2.2, color=t["cover_text"], base=46,
           anchor=MSO_ANCHOR.BOTTOM)
    _deco(slide, MSO_SHAPE.RECTANGLE, 1.1, 4.42, 1.2, 0.07, t["accent"])
    sub = s["subtitle"]
    if closing and s["bullets"]:
        sub = "\n".join([x for x in [sub] + [b.strip() for b in s["bullets"]] if x])
    body = next((p for p in slide.placeholders if p.placeholder_format.idx == 1), None)
    if body is not None and sub:
        _place(body, 1.1, 4.7, tw, 1.7)
        _frame(body.text_frame, MSO_ANCHOR.TOP)
        body.text_frame.text = sub
        size = _fit(sub.split("\n"), tw, 1.7, 20, 13, gap_pt=4)
        for p in body.text_frame.paragraphs:
            p.alignment = PP_ALIGN.LEFT
            p.space_after = Pt(4)
            _style_runs(p, t["body"], size, t["cover_muted"])
    elif body is not None:
        body._element.getparent().remove(body._element)


def _section(slide, s, t):
    _bg(slide, t["cover_bg"])
    _deco(slide, MSO_SHAPE.RECTANGLE, 0, 0, 0.28, H, t["accent"])
    if t["motif"] == "circles":
        _deco(slide, MSO_SHAPE.OVAL, 9.4, 1.2, 5.2, 5.2, t["accent2"], alpha=22)
    _deco(slide, MSO_SHAPE.RECTANGLE, 1.1, 3.0, 1.2, 0.07, t["accent"])
    _title(slide, s["title"], t, 1.1, 3.2, 9.0, 1.5, color=t["cover_text"], base=40, anchor=MSO_ANCHOR.TOP)
    body = next((p for p in slide.placeholders if p.placeholder_format.idx == 1), None)
    if body is not None and s["subtitle"]:
        _place(body, 1.1, 4.8, 9.0, 1.0)
        _frame(body.text_frame, MSO_ANCHOR.TOP)
        body.text_frame.text = s["subtitle"]
        for p in body.text_frame.paragraphs:
            _style_runs(p, t["body"], 20, t["cover_muted"])
    elif body is not None:
        body._element.getparent().remove(body._element)


def _full(slide, s, t):
    """Full-bleed picture with a dark scrim; the title (and subtitle) sit over it."""
    _bg(slide, t["cover_bg"])
    pic = _picture(slide, s, 0, 0, W, H, name="Background image")
    scrim = _deco(slide, MSO_SHAPE.RECTANGLE, 0, 0, W, H, "000000", alpha=48, back=False)
    for shp in (scrim, pic):                           # behind the placeholders, picture lowest
        if shp is not None:
            _back(shp)
    _deco(slide, MSO_SHAPE.RECTANGLE, 1.1, 4.55, 1.2, 0.07, t["accent"], back=False)
    _title(slide, s["title"], t, 1.1, 2.4, 10.5, 2.0, color="FFFFFF", base=44, anchor=MSO_ANCHOR.BOTTOM)
    body = next((p for p in slide.placeholders if p.placeholder_format.idx == 1), None)
    if body is not None and s["subtitle"]:
        _place(body, 1.1, 4.8, 10.5, 1.2)
        _frame(body.text_frame, MSO_ANCHOR.TOP)
        body.text_frame.text = s["subtitle"]
        for p in body.text_frame.paragraphs:
            _style_runs(p, t["body"], 20, "E5E7EB")
    elif body is not None:
        body._element.getparent().remove(body._element)
    _credit(slide, s, t, W - MX - 6, 7.05, 6, color="E5E7EB")


def _bullets(slide, s, t):
    body = next((p for p in slide.placeholders if p.placeholder_format.idx == 1), None)
    items = s["bullets"]
    h = 4.85
    _deco(slide, MSO_SHAPE.RECTANGLE, MX, 1.95, 0.06, min(h - 0.2, 0.62 * max(len(items), 3)), t["accent"])
    if s.get("image_data"):                           # text on the left, picture on the right
        px, pw = 7.55, W - MX - 7.55
        _fill_bullets(body, items, t, MX + 0.3, 1.9, px - MX - 0.7, h, base=26 if len(items) <= 4 else 22, floor=12)
        _picture(slide, s, px, 1.95, pw, h - 0.35)
        _credit(slide, s, t, px, 1.95 + h - 0.3, pw)
        return
    _fill_bullets(body, items, t, MX + 0.3, 1.9, W - 2 * MX - 0.3, h, base=30 if len(items) <= 4 else 26)


def _columns(slide, s, t):
    tops = O.groups(s["bullets"])
    half = math.ceil(len(tops) / 2)
    parts = [tops[:half], tops[half:]]
    bodies = [p for p in slide.placeholders if p.placeholder_format.idx in (1, 2)]
    bodies.sort(key=lambda p: p.placeholder_format.idx)
    cw = (W - 2 * MX - 0.6) / 2
    for i, (ph, part) in enumerate(zip(bodies, parts)):
        lines = []
        for head, kids in part:
            lines.append(head)
            lines += ["\t" + k for k in kids]
        x = MX + i * (cw + 0.6)
        _fill_bullets(ph, lines, t, x, 1.95, cw, 4.75, base=21, floor=12)
    _deco(slide, MSO_SHAPE.RECTANGLE, W / 2 - 0.01, 2.0, 0.02, 4.4, t["line"])


def _cards(slide, s, t):
    items = [_heading_body(g) for g in O.groups(s["bullets"])]
    n = len(items)
    cols = n if n <= 4 else 3
    rows = math.ceil(n / cols)
    gap = 0.3
    area_w, area_h, y0 = W - 2 * MX, 4.75, 1.95
    cw = min((area_w - gap * (cols - 1)) / cols, 4.2 if cols == 2 else 9)
    x0 = MX + (area_w - (cw * cols + gap * (cols - 1))) / 2 if cols == 2 else MX
    ch = min((area_h - gap * (rows - 1)) / rows, 3.7)
    for i, (head, kids) in enumerate(items):
        r, c = divmod(i, cols)
        x, y = x0 + c * (cw + gap), y0 + r * (ch + gap)
        card = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x), Inches(y), Inches(cw), Inches(ch))
        card.name = f"Card {i + 1}"
        card.fill.solid()
        card.fill.fore_color.rgb = _rgb(t["surface"])
        card.line.color.rgb = _rgb(t["line"])
        card.line.width = Pt(1)
        card.shadow.inherit = False
        accent = t["accent"] if i % 2 == 0 else t["accent2"]
        _deco(slide, MSO_SHAPE.RECTANGLE, x, y, cw, 0.08, accent, back=False)
        badge = _deco(slide, MSO_SHAPE.OVAL, x + 0.28, y + 0.32, 0.5, 0.5, accent, back=False)
        bt = badge.text_frame
        bt.text = str(i + 1)
        _frame(bt, MSO_ANCHOR.MIDDLE)
        bt.paragraphs[0].alignment = PP_ALIGN.CENTER
        _style_runs(bt.paragraphs[0], t["head"], 15, _on(accent), True)
        tf = card.text_frame
        _frame(tf, MSO_ANCHOR.TOP, (0.28, 1.0, 0.28, 0.2))
        inner_w, inner_h = cw - 0.56, ch - 1.2
        hs = _fit([head], inner_w, 0.9, 21, 14, gap_pt=0)
        bs = _fit(kids, inner_w, max(inner_h - 0.8, 0.8), 18, 11, gap_pt=6)
        tf.text = head
        tf.paragraphs[0].alignment = PP_ALIGN.LEFT
        tf.paragraphs[0].space_after = Pt(8)
        _style_runs(tf.paragraphs[0], t["head"], hs, t["text"], True)
        for k in kids:
            p = tf.add_paragraph()
            p.text = k
            p.alignment = PP_ALIGN.LEFT
            p.space_after = Pt(6)
            _style_runs(p, t["body"], bs, t["muted"])


def _stats(slide, s, t):
    items = [(O.stat_split(g[0]) or (g[0], "")) for g in O.groups(s["bullets"])]
    n = len(items)
    gap = 0.35
    area_w = W - 2 * MX
    cw = (area_w - gap * (n - 1)) / n
    y, ch = 2.2, 3.7
    for i, (num, cap) in enumerate(items):
        x = MX + i * (cw + gap)
        tile = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x), Inches(y), Inches(cw), Inches(ch))
        tile.name = f"Stat {i + 1}"
        tile.fill.solid()
        tile.fill.fore_color.rgb = _rgb(t["surface"])
        tile.line.color.rgb = _rgb(t["line"])
        tile.line.width = Pt(1)
        tile.shadow.inherit = False
        accent = t["accent"] if i % 2 == 0 else t["accent2"]
        _deco(slide, MSO_SHAPE.RECTANGLE, x, y, 0.1, ch, accent, back=False)
        tf = tile.text_frame
        _frame(tf, MSO_ANCHOR.MIDDLE, (0.45, 0.2, 0.3, 0.2))
        ns = _fit([num], cw - 0.75, 1.4, 60, 28, gap_pt=0)
        tf.text = num
        tf.paragraphs[0].alignment = PP_ALIGN.LEFT
        tf.paragraphs[0].space_after = Pt(10)
        _style_runs(tf.paragraphs[0], t["head"], ns, accent if _lum(accent) < 0.75 or _lum(t["surface"]) < 0.5 else t["text"], True)
        p = tf.add_paragraph()
        p.text = cap
        p.alignment = PP_ALIGN.LEFT
        _style_runs(p, t["body"], _fit([cap], cw - 0.75, 1.2, 18, 12, gap_pt=0), t["text"])


def _timeline(slide, s, t):
    items = [_heading_body(g) for g in O.groups(s["bullets"])]
    n = len(items)
    area_w = W - 2 * MX
    sw = area_w / n
    ly = 3.0
    _deco(slide, MSO_SHAPE.RECTANGLE, MX + sw / 2, ly - 0.02, area_w - sw, 0.04, t["line"] if _lum(t["line"]) < 0.9 else t["muted"])
    for i, (head, kids) in enumerate(items):
        cx = MX + sw * i + sw / 2
        dot = _deco(slide, MSO_SHAPE.OVAL, cx - 0.3, ly - 0.3, 0.6, 0.6,
                    t["accent"] if i % 2 == 0 else t["accent2"], back=False)
        dt = dot.text_frame
        dt.text = str(i + 1)
        _frame(dt, MSO_ANCHOR.MIDDLE)
        dt.paragraphs[0].alignment = PP_ALIGN.CENTER
        _style_runs(dt.paragraphs[0], t["head"], 16, _on(t["accent"] if i % 2 == 0 else t["accent2"]), True)
        w = sw - 0.3
        paras = [(head, t["head"], _fit([head], w, 0.9, 22, 13, gap_pt=0), t["text"], True, False, 6)]
        bs = _fit(kids, w, 2.6, 18, 11, gap_pt=6)
        paras += [(k, t["body"], bs, t["muted"], False, False, 6) for k in kids]
        _textbox(slide, cx - w / 2, ly + 0.6, w, 3.2, paras, name=f"Step {i + 1}", align=PP_ALIGN.CENTER)


def _quote(slide, s, t):
    items = [b.strip() for b in s["bullets"]]
    quote = items[0].strip('"“”')
    by = " — ".join(items[1:])
    _deco(slide, MSO_SHAPE.RECTANGLE, MX + 0.2, 2.3, 0.08, 3.4, t["accent"])
    _textbox(slide, MX + 0.7, 2.2, W - 2 * MX - 1.2, 3.0,
             [("“" + quote + "”", t["head"], _fit([quote], W - 2 * MX - 1.2, 3.0, 34, 18, gap_pt=0),
               t["text"], False, True, 0)], name="Quote", anchor=MSO_ANCHOR.MIDDLE)
    if by:
        _textbox(slide, MX + 0.7, 5.4, W - 2 * MX - 1.2, 0.5,
                 [(by, t["body"], 16, t["accent"], True, False, 0)], name="Attribution")


def _table(slide, s, t):
    rows = s["table"]
    cols = max(len(r) for r in rows)
    rows = [r + [""] * (cols - len(r)) for r in rows]
    h = min(0.72 * len(rows), 4.7)
    gf = slide.shapes.add_table(len(rows), cols, Inches(MX), Inches(1.95), Inches(W - 2 * MX), Inches(h))
    gf.name = "Table"
    tbl = gf.table
    tbl.first_row = True
    tbl.horz_banding = False
    size = 20 if cols <= 3 and len(rows) <= 5 else (16 if len(rows) <= 7 else 13)
    for r, row in enumerate(rows):
        for c, val in enumerate(row):
            cell = tbl.cell(r, c)
            cell.fill.solid()
            head = r == 0
            fill = t["accent"] if head else (t["surface"] if r % 2 else t["bg"])
            cell.fill.fore_color.rgb = _rgb(fill)
            cell.margin_left = cell.margin_right = Inches(0.15)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            cell.text_frame.text = val
            _style_runs(cell.text_frame.paragraphs[0], t["body"], size, _on(fill) if head else t["text"], head)


_LAYOUT_FOR = {"cover": "Title Slide", "closing": "Title Slide", "section": "Section Header", "full": "Section Header",
               "bullets": "Title and Content", "columns": "Two Content"}


def build(deck: dict) -> Presentation:
    t = O.resolve_theme(deck)
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(W), Inches(H)
    lays = {l.name: l for l in prs.slide_layouts}
    deck_title = deck["title"]
    for i, s in enumerate(deck["slides"]):
        kind = s["kind"]
        slide = prs.slides.add_slide(lays[_LAYOUT_FOR.get(kind, "Title Only")])
        if kind in ("cover", "closing"):
            _cover(slide, s, t, closing=kind == "closing")
        elif kind == "full" or (kind == "section" and s.get("image_data")):
            _full(slide, s, t)
        elif kind == "section":
            _section(slide, s, t)
        else:
            _chrome(slide, t, s["title"], deck_title, i + 1)
            {"bullets": _bullets, "columns": _columns, "cards": _cards, "stats": _stats,
             "timeline": _timeline, "quote": _quote, "table": _table}[kind](slide, s, t)
        _drop_empty_placeholders(slide)
        if s["notes"]:
            slide.notes_slide.notes_text_frame.text = s["notes"]
    return prs
