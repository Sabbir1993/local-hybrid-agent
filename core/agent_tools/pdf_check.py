"""Quality check of a finished PDF (the PDF counterpart of the deck layout check): blank pages, a heading stranded at the
bottom of a page, a page that is almost empty in the middle of the document, and a missing footer page number.

Uses pdfplumber for the text and pypdfium2 to rasterise (both ship with the app's PDF tooling). Any failure to check
returns no findings: the check must never block or break a build.
"""
import io
import re
from typing import Optional

BLANK_INK = 0.0015          # fraction of non-white pixels in the page body below which a page counts as blank
SPARSE_INK = 0.012          # ... and below which a page in the middle of the document is "almost empty"
_PAGE_NO = re.compile(r"Page\s+\d+\s+of\s+\d+")


def headings_of(markdown: str) -> list:
    out, fence = [], False
    for line in (markdown or "").splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
            continue
        m = None if fence else re.match(r"^#{1,3}\s+(.+?)\s*#*\s*$", line)
        if m:
            out.append(re.sub(r"[*_`]", "", m.group(1)).strip())
    return out


def _ink_ratios(data: bytes) -> list:
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(data)
    ratios = []
    try:
        for i in range(len(pdf)):
            img = pdf[i].render(scale=0.35).to_pil().convert("L")
            w, h = img.size
            body = img.crop((0, int(h * 0.04), w, int(h * 0.92)))      # without the footer strip
            px = body.tobytes()
            ratios.append(sum(1 for v in px if v < 235) / max(1, len(px)))
    finally:
        pdf.close()
    return ratios


def check_pdf(data: bytes, markdown: str = "") -> list:
    """Short sentences about problems in the PDF, [] when it looks fine (or cannot be checked)."""
    try:
        import pdfplumber
        findings: list = []
        heads = {h.lower() for h in headings_of(markdown) if h}
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            n = len(pdf.pages)
            texts = [(pg.extract_text() or "") for pg in pdf.pages]
        ratios = _ink_ratios(data)
        for i, (text, ink) in enumerate(zip(texts, ratios), 1):
            lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not _PAGE_NO.search(ln)]
            if ink < BLANK_INK and not lines:
                findings.append(f"page {i} is blank")
                continue
            if 1 < i < n and ink < SPARSE_INK and len(lines) <= 2:
                findings.append(f"page {i} is almost empty")
            if lines and heads and lines[-1].strip().lower() in heads and i < n:
                findings.append(f"the heading '{lines[-1][:40]}' is stranded at the bottom of page {i}")
        return findings[:8]
    except Exception:
        return []


def summary(findings: list) -> str:
    return f"PDF check found {len(findings)} issue(s): " + "; ".join(findings) + "." if findings else ""


def render_note(data: Optional[bytes], markdown: str) -> str:
    return summary(check_pdf(data, markdown)) if data else ""
