"""Page operations on PDFs with pypdf (+ reportlab for text overlays): merge, split, extract, delete, reorder, rotate, page
numbers, watermark, metadata. Works on copies in memory; the caller saves the result(s) as new files."""
import io
import re
from typing import Optional

from .common import MAX_PAGES, PdfError, open_reader, parse_pages

MAX_SPLIT_FILES = 40
OPS = ("merge", "split", "extract", "delete", "reorder", "rotate", "add_page_numbers", "watermark", "set_metadata")


def _overlay(width: float, height: float, draw) -> "object":
    """A one-page PDF (reportlab) the size of the target page; `draw(canvas, w, h)` paints on it."""
    from pypdf import PdfReader
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(width, height))
    draw(c, width, height)
    c.showPage()
    c.save()
    buf.seek(0)
    return PdfReader(buf).pages[0]


def _attach(pages: list) -> list:
    """Pages bound to a writer: pypdf only merges overlays reliably into pages that belong to one."""
    from pypdf import PdfWriter
    w = PdfWriter()
    return [w.add_page(p) for p in pages]


def _safe_text(text: str) -> str:
    """The built-in PDF fonts cover Latin-1 only; anything else would print as boxes, so refuse instead of shipping boxes."""
    try:
        str(text).encode("latin-1")
    except UnicodeEncodeError:
        raise PdfError("watermark / page-number text must use Latin letters (the built-in PDF fonts have no Bengali glyphs); "
                       "use English text or build the stamp into a PDF with doc_create")
    return str(text)


def run(files: list, ops: list, password: Optional[str] = None) -> tuple:
    """files: [(name, bytes)] (the first is the base document). -> ([(bytes, tag)], [change descriptions])
    A split makes several outputs; every other op keeps working on the same page list."""
    from pypdf import PdfWriter
    if not files:
        raise PdfError("file required")
    if not isinstance(ops, list) or not ops:
        raise PdfError(f"ops required: a list like [{{\"op\": \"extract\", \"pages\": \"1-3\"}}] (ops: {', '.join(OPS)})")
    if len(ops) > 20:
        raise PdfError("at most 20 operations per call")
    readers = {name: open_reader(data, password) for name, data in files}
    base_name = files[0][0]
    pages = list(readers[base_name].pages)
    meta = None
    changes = []
    outputs = None
    start_total = len(pages)

    for op in ops:
        kind = str((op or {}).get("op") or "").lower()
        n = len(pages)
        if kind == "merge":
            extra = [str(x) for x in (op.get("files") or ([op["file"]] if op.get("file") else []))]
            if not extra:
                raise PdfError("merge: files required (the other PDFs to append, in order)")
            added = 0
            for name in extra:
                if name not in readers:
                    raise PdfError(f"merge: '{name}' was not loaded")
                pages += list(readers[name].pages)
                added += len(readers[name].pages)
            changes.append(f"merged {len(extra)} file(s), +{added} pages")
        elif kind in ("extract", "delete"):
            sel = parse_pages(op.get("pages"), n)
            if kind == "extract":
                pages = [pages[i - 1] for i in sel]
                changes.append(f"kept {len(sel)} page(s)")
            else:
                drop = set(sel)
                if len(drop) >= n:
                    raise PdfError("delete: that would remove every page")
                pages = [p for i, p in enumerate(pages, 1) if i not in drop]
                changes.append(f"removed page(s) {', '.join(map(str, sorted(drop)))}")
        elif kind == "reorder":
            order = parse_pages(op.get("order") or op.get("pages"), n)
            if sorted(order) != list(range(1, n + 1)):
                raise PdfError(f"reorder: give every page exactly once (1..{n}), e.g. order='3,1,2'")
            pages = [pages[i - 1] for i in order]
            changes.append("pages reordered")
        elif kind == "rotate":
            sel = parse_pages(op.get("pages"), n)
            deg = int(op.get("degrees") or 90)
            if deg % 90:
                raise PdfError("rotate: degrees must be a multiple of 90")
            for i in sel:
                pages[i - 1].rotate(deg)
            changes.append(f"rotated {len(sel)} page(s) by {deg}")
        elif kind == "add_page_numbers":
            pages = _attach(pages)
            fmt = _safe_text(op.get("format") or "Page {n} of {total}")
            pos = str(op.get("position") or "bottom-center").lower()
            total = len(pages)

            for i, p in enumerate(pages, 1):
                w, h = float(p.mediabox.width), float(p.mediabox.height)

                def draw(c, w, h, i=i):
                    c.setFont("Helvetica", 9)
                    c.setFillGray(0.35)
                    txt = fmt.replace("{n}", str(i)).replace("{total}", str(total))
                    y = 20 if "bottom" in pos else h - 28
                    x = w - 40 if "right" in pos else (40 if "left" in pos else w / 2)
                    (c.drawRightString if "right" in pos else (c.drawString if "left" in pos else c.drawCentredString))(x, y, txt)
                p.merge_page(_overlay(w, h, draw))
            changes.append("page numbers added")
        elif kind == "watermark":
            pages = _attach(pages)
            text = _safe_text(op.get("text") or "")
            if not text.strip():
                raise PdfError("watermark: text required")
            sel = set(parse_pages(op.get("pages"), n))
            opacity = min(max(float(op.get("opacity") or 0.18), 0.03), 0.9)
            size = float(op.get("size") or 64)
            for i, p in enumerate(pages, 1):
                if i not in sel:
                    continue
                w, h = float(p.mediabox.width), float(p.mediabox.height)

                def draw(c, w, h):
                    c.saveState()
                    c.setFillColorRGB(0.5, 0.5, 0.5)
                    c.setFillAlpha(opacity)
                    c.setFont("Helvetica-Bold", size)
                    c.translate(w / 2, h / 2)
                    c.rotate(float(op.get("angle") or 45))
                    c.drawCentredString(0, 0, text)
                    c.restoreState()
                p.merge_page(_overlay(w, h, draw))
            changes.append(f"watermark '{text[:30]}' on {len(sel)} page(s)")
        elif kind == "set_metadata":
            meta = {f"/{k.title()}": str(v)[:200] for k, v in op.items()
                    if k in ("title", "author", "subject", "keywords") and v}
            changes.append("metadata set")
        elif kind == "split":
            ranges = op.get("ranges")
            if op.get("every_n"):
                k = max(1, int(op["every_n"]))
                parts = [list(range(i, min(i + k, n + 1))) for i in range(1, n + 1, k)]
            elif ranges:
                parts = [parse_pages(r, n) for r in re.split(r"[;|]", str(ranges)) if r.strip()]
            else:
                raise PdfError("split: ranges ('1-3;4-6') or every_n required")
            if len(parts) > MAX_SPLIT_FILES:
                raise PdfError(f"split: at most {MAX_SPLIT_FILES} files")
            outputs = []
            for k, part in enumerate(parts, 1):
                w = PdfWriter()
                for i in part:
                    w.add_page(pages[i - 1])
                buf = io.BytesIO()
                w.write(buf)
                outputs.append((buf.getvalue(), f"part{k}"))
            changes.append(f"split into {len(parts)} file(s)")
        else:
            raise PdfError(f"unknown operation '{kind}' (ops: {', '.join(OPS)})")
        if len(pages) > MAX_PAGES:
            raise PdfError(f"more than {MAX_PAGES} pages")

    if outputs is None:
        w = PdfWriter()
        for p in pages:
            w.add_page(p)
        if meta:
            w.add_metadata(meta)
        buf = io.BytesIO()
        w.write(buf)
        outputs = [(buf.getvalue(), "edited")]
    changes.insert(0, f"{start_total} page(s) -> {len(pages)}" if len(outputs) == 1 else f"{start_total} page(s) in")
    return outputs, changes
