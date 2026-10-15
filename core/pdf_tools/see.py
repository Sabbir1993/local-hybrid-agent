"""Looking at PDF pages: render them locally (pypdfium2) and ask the vision model about them. For charts, layouts, stamps,
handwriting-like content and scanned pages. The pages are drawn on this machine; they go to the vision lane only, and by default
only to a LOCAL one (a page image can carry card numbers that no text guard can see) unless `chat.pdf_see_cloud` is true."""
import io
from typing import Optional

from .common import MAX_TEXT_CHARS, PdfError, mask, open_reader, parse_pages, render_pages

MAX_PAGES_PER_CALL = 4
MAX_DIM = 1400


def _jpeg(img) -> bytes:
    img = img.convert("RGB")
    if max(img.size) > MAX_DIM:
        k = MAX_DIM / max(img.size)
        img = img.resize((int(img.width * k), int(img.height * k)))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def cloud_allowed() -> bool:
    try:
        from ..small_model import APP_CONFIG
        return bool((APP_CONFIG.get("chat") or {}).get("pdf_see_cloud", False))
    except Exception:
        return False


async def see(data: bytes, pages=None, question: str = "", password: Optional[str] = None) -> str:
    from ..small_model.vision import describe_image_bytes
    n_pages = len(open_reader(data, password).pages)
    want = parse_pages(pages or "1", n_pages)[:MAX_PAGES_PER_CALL]
    q = (question or "Describe this page: its layout, headings, tables, charts and any text that matters.").strip()[:600]
    out = []
    for n, img in render_pages(data, want, 1.5, password):
        desc = await describe_image_bytes(_jpeg(img), q + f" (This is page {n} of {n_pages}.)", force_local=not cloud_allowed())
        if str(desc).startswith("error:"):
            if not out:
                return str(desc) + " - load a vision model (Models page) to let me look at pages."
            out.append(f"--- page {n} ---\n{desc}")
            break
        out.append(f"--- page {n} ---\n{desc}")
    skipped = len(parse_pages(pages or "1", n_pages)) - len(want)
    text = "\n\n".join(out)
    if skipped > 0:
        text += f"\n\n({skipped} more page(s) selected; ask again with those pages - at most {MAX_PAGES_PER_CALL} are looked at per call)"
    return mask(text[:MAX_TEXT_CHARS])

