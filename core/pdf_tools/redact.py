"""True redaction. A black box drawn over text still leaves the text in the file, so every page that has a match is redrawn
as a picture with the matches painted out (the text layer of THOSE pages is gone, other pages are untouched), and the result is
checked: if any redacted term can still be extracted from the output, nothing is delivered.

Card numbers are a built-in target (`pan: true`): any 13-19 digit run that passes the Luhn check, with or without separators.
"""
import io
import re
from typing import Optional

from .common import PdfError, open_reader, parse_pages, render_pages

SCALE = 2.4                       # ~170 dpi for the redrawn pages
_PAN_RX = r"(?:\d[ -]?){13,19}"
MAX_TERMS = 40


def _luhn(digits: str) -> bool:
    s, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        s += d
        alt = not alt
    return s % 10 == 0


def _hits(page, terms: list, regexes: list, pan: bool) -> list:
    """[(x0, top, x1, bottom, kind)] in PDF points (pdfplumber coordinates)."""
    found = []
    for t in terms:
        for m in page.search(re.escape(t), regex=True, case=False) or []:
            found.append((m["x0"], m["top"], m["x1"], m["bottom"], "term"))
    for rx in regexes:
        for m in page.search(rx, regex=True, case=False) or []:
            found.append((m["x0"], m["top"], m["x1"], m["bottom"], "pattern"))
    if pan:
        for m in page.search(_PAN_RX, regex=True) or []:
            digits = re.sub(r"\D", "", m["text"])
            if 13 <= len(digits) <= 19 and _luhn(digits):
                found.append((m["x0"], m["top"], m["x1"], m["bottom"], "card number"))
    return found


def redact(data: bytes, terms=None, patterns=None, pan: bool = False, pages=None, password: Optional[str] = None) -> tuple:
    """-> (pdf bytes, report text). Raises PdfError when nothing matched or the check fails."""
    import pdfplumber
    from PIL import ImageDraw
    from pypdf import PdfReader, PdfWriter
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    terms = [str(t) for t in (terms or []) if str(t).strip()][:MAX_TERMS]
    regexes = [str(p) for p in (patterns or []) if str(p).strip()][:MAX_TERMS]
    for rx in regexes:
        try:
            re.compile(rx)
        except re.error:
            raise PdfError(f"pattern '{rx}' is not a valid regular expression")
    if not (terms or regexes or pan):
        raise PdfError("nothing to redact: give terms, patterns or pan=true")
    reader = open_reader(data, password)
    want = parse_pages(pages, len(reader.pages))
    per_page = {}
    with pdfplumber.open(io.BytesIO(data), password=password or None) as pdf:
        for n in want:
            page = pdf.pages[n - 1]
            hits = _hits(page, terms, regexes, pan)
            if hits:
                per_page[n] = (float(page.width), float(page.height), hits)
    if not per_page:
        raise PdfError("no match found: the file was not changed (scanned pages have no text to match: run pdf_ocr first)")
    drawn = dict(render_pages(data, sorted(per_page), SCALE, password))
    writer = PdfWriter()
    for i, src in enumerate(reader.pages, 1):
        if i not in per_page:
            writer.add_page(src)
            continue
        w_pt, h_pt, hits = per_page[i]
        img = drawn[i].convert("RGB")
        sx, sy = img.width / w_pt, img.height / h_pt
        d = ImageDraw.Draw(img)
        for x0, top, x1, bottom, _k in hits:
            d.rectangle([(x0 - 1.5) * sx, (top - 1.5) * sy, (x1 + 1.5) * sx, (bottom + 1.5) * sy], fill="black")
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=(w_pt, h_pt))
        c.drawImage(ImageReader(img), 0, 0, width=w_pt, height=h_pt)
        c.showPage()
        c.save()
        buf.seek(0)
        writer.add_page(PdfReader(buf).pages[0])
    out = io.BytesIO()
    writer.write(out)
    result = out.getvalue()

    # the check that makes it a redaction: nothing that was asked to go may still be extractable from the output
    with pdfplumber.open(io.BytesIO(result)) as chk:
        for n in per_page:
            if _hits(chk.pages[n - 1], terms, regexes, pan):
                raise PdfError("redaction check failed: some text is still extractable, so nothing was saved")
    raw = result.lower()
    for t in terms:
        if t.lower().encode("latin-1", "ignore") and len(t) >= 4 and t.lower().encode("latin-1", "ignore") in raw:
            raise PdfError("redaction check failed: a term is still present in the file bytes, so nothing was saved")
    kinds = {}
    for _n, (_w, _h, hits) in per_page.items():
        for h in hits:
            kinds[h[4]] = kinds.get(h[4], 0) + 1
    report = (f"Redacted {sum(kinds.values())} item(s) on {len(per_page)} page(s) "
              f"(page {', '.join(map(str, sorted(per_page)))}): "
              + ", ".join(f"{v} {k}" for k, v in kinds.items())
              + ". Verified: the redacted text can no longer be extracted. Those pages are now pictures (not selectable).")
    return result, report
