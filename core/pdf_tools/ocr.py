"""OCR for scanned PDFs. Tesseract when it is installed (a plain local program, found on PATH or at its usual Windows path; a
searchable PDF with an invisible text layer comes out), else the vision model transcribes the pages (text only, labelled as
such), else a clear message about how to enable OCR. The command line is fixed: only a validated language code and temp file
names are ever passed, never user text."""
import io
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from .common import MAX_TEXT_CHARS, PdfError, mask, open_reader, parse_pages, render_pages

MAX_OCR_PAGES = 60
PAGE_TIMEOUT_S = 120
_WIN = (r"C:\Program Files\Tesseract-OCR\tesseract.exe", r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe")
_LANG = re.compile(r"^[a-z]{3}(?:_[a-z]+)?(?:\+[a-z]{3}(?:_[a-z]+)?)*$")


def enabled() -> bool:
    try:
        from ..small_model import APP_CONFIG
        return (APP_CONFIG.get("chat") or {}).get("pdf_ocr", True) is not False
    except Exception:
        return True


def find_tesseract() -> Optional[str]:
    hit = shutil.which("tesseract")
    if hit:
        return hit
    return next((p for p in _WIN if os.path.isfile(p)), None)


def languages(exe: str) -> list:
    try:
        r = subprocess.run([exe, "--list-langs"], capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
        return [ln.strip() for ln in r.stdout.splitlines()[1:] if ln.strip()]
    except Exception:
        return []


def check_lang(lang: Optional[str], available: list) -> str:
    lang = (lang or "").strip() or ("eng+ben" if "ben" in available and "eng" in available else "eng")
    if not _LANG.match(lang):
        raise PdfError("lang must look like eng or eng+ben")
    missing = [x for x in lang.split("+") if available and x not in available]
    if missing:
        raise PdfError(f"the OCR language pack '{missing[0]}' is not installed (installed: {', '.join(available[:12])})")
    return lang


def ocr_with_tesseract(exe: str, data: bytes, pages: list, lang: str, password: Optional[str] = None) -> tuple:
    """-> (searchable pdf bytes with the OCR'd pages replaced, extracted text by page)."""
    from pypdf import PdfReader, PdfWriter
    reader = open_reader(data, password)
    rendered = dict(render_pages(data, pages, 4.1, password))          # ~300 dpi
    texts, page_pdfs = {}, {}
    with tempfile.TemporaryDirectory(prefix="pdfocr_") as d:
        for n in pages:
            png = Path(d) / f"p{n}.png"
            rendered[n].convert("RGB").save(png)
            base = Path(d) / f"o{n}"
            subprocess.run([exe, str(png), str(base), "-l", lang, "pdf"], check=True, capture_output=True,
                           timeout=PAGE_TIMEOUT_S, stdin=subprocess.DEVNULL, cwd=d)
            produced = Path(str(base) + ".pdf")
            if produced.is_file():
                page_pdfs[n] = produced.read_bytes()
                from pdfplumber import open as plumber_open
                with plumber_open(io.BytesIO(page_pdfs[n])) as pp:
                    texts[n] = (pp.pages[0].extract_text() or "").strip()
    w = PdfWriter()
    for i, src in enumerate(reader.pages, 1):
        if i in page_pdfs:
            w.add_page(PdfReader(io.BytesIO(page_pdfs[i])).pages[0])
        else:
            w.add_page(src)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue(), texts


async def ocr_with_vision(data: bytes, pages: list, password: Optional[str] = None) -> dict:
    from .see import _jpeg, cloud_allowed
    from ..small_model.vision import describe_image_bytes
    out = {}
    for n, img in render_pages(data, pages[:12], 2.0, password):
        desc = await describe_image_bytes(
            _jpeg(img), "Transcribe ALL the text on this page exactly as written, in reading order. Keep table rows on one line "
                        "separated by ' | '. Output only the transcription.", force_local=not cloud_allowed())
        if str(desc).startswith("error:"):
            raise PdfError(str(desc))
        out[n] = str(desc).strip()
    return out


def render_text(texts: dict) -> str:
    return mask("\n\n".join(f"--- page {n} ---\n{t or '(no text recognised)'}" for n, t in sorted(texts.items()))[:MAX_TEXT_CHARS])


def how_to_enable() -> str:
    return ("OCR is not available here: install Tesseract (a free local program; include the English and Bengali language packs) "
            "and restart the server, or load a vision model and use pdf_see for the pages.")


def selected_pages(data: bytes, pages, password: Optional[str] = None) -> list:
    n = len(open_reader(data, password).pages)
    want = parse_pages(pages, n) if pages else None
    if want is None:                                  # default: the pages that have no text layer
        from .read import info
        want = info(data, password)["scanned_pages"] or list(range(1, n + 1))
    if len(want) > MAX_OCR_PAGES:
        raise PdfError(f"at most {MAX_OCR_PAGES} pages per call: choose a page range")
    return want

