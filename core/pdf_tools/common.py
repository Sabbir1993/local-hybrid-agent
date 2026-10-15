"""Shared helpers for the PDF tools: finding the file, page-range syntax, opening (with a password), saving a result."""
import io
import re
import uuid
from pathlib import Path
from typing import Optional

MAX_BYTES = 120 * 1024 * 1024
MAX_PAGES = 3000
MAX_TEXT_CHARS = 12000


class PdfError(ValueError):
    """A problem the model / user can act on (bad page range, wrong password, not a PDF ...)."""


def resolve(name: str) -> Path:
    from ..doc_tools.location import resolve_common
    p = resolve_common(str(name or "").strip())
    if p is None or not p.is_file():
        raise PdfError(f"no file named '{name}' - use the name from the attachment note or the download link")
    if p.suffix.lower() != ".pdf":
        raise PdfError(f"'{p.name}' is not a PDF")
    if p.stat().st_size > MAX_BYTES:
        raise PdfError(f"'{p.name}' is larger than {MAX_BYTES // (1024 * 1024)} MB")
    return p


def parse_pages(spec, total: int) -> list:
    """'1-3,7,last' / 'all' / 'odd' / 'even' / [1, 2] -> 1-based page numbers in the order written, each checked against `total`."""
    if spec in (None, "", "all"):
        return list(range(1, total + 1))
    if isinstance(spec, (list, tuple)):
        parts = [str(x) for x in spec]
    else:
        parts = [p.strip() for p in str(spec).replace(";", ",").split(",") if p.strip()]
    out = []
    for p in parts:
        low = p.lower()
        if low in ("odd", "even"):
            out += [n for n in range(1, total + 1) if (n % 2 == 1) == (low == "odd")]
            continue
        low = low.replace("last", str(total)).replace("end", str(total))
        m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", low)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > b:
                a, b = b, a
            out += list(range(a, b + 1))
        elif re.fullmatch(r"\d+", low):
            out.append(int(low))
        else:
            raise PdfError(f"cannot read the page range '{p}' (use 1-3,7,last)")
    bad = [n for n in out if not 1 <= n <= total]
    if bad:
        raise PdfError(f"page {bad[0]} is outside this document (it has {total} page{'s' if total != 1 else ''})")
    return out


def open_reader(data: bytes, password: Optional[str] = None):
    """pypdf reader; an encrypted file needs the password (it is used for this call only and never stored)."""
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError
    try:
        reader = PdfReader(io.BytesIO(data))
    except (PdfReadError, ValueError, OSError) as e:
        raise PdfError(f"this does not look like a readable PDF ({type(e).__name__})")
    if reader.is_encrypted:
        if not password:
            raise PdfError("this PDF is password protected: give the password (password=...)")
        if not reader.decrypt(password):
            raise PdfError("the password does not open this PDF")
    if len(reader.pages) > MAX_PAGES:
        raise PdfError(f"more than {MAX_PAGES} pages")
    return reader


def mask(text: str) -> str:
    """Card numbers never leave a tool as text."""
    from ..pan import mask_pans
    return mask_pans(text)[0]


def save_new(data: bytes, source_name: str, tag: str, suffix: str = ".pdf") -> str:
    """Write `data` as a new file next to the user's other files and register it; returns its name."""
    from .. import doc_store
    from ..agent_tools.workspace import _common_resolve
    from ..doc_ops.base import sha256
    from ..request_context import get_current_user_id
    stem = re.sub(r"([-_][0-9a-fA-F]{8})+$", "", Path(source_name).stem)
    name = f"{stem}-{tag}-{uuid.uuid4().hex[:8]}{suffix}"
    p = _common_resolve(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    try:
        doc_store.register(get_current_user_id(), p.name, suffix.lstrip("."), sha256=sha256(data))
    except Exception:
        pass
    return p.name


def render_pages(data: bytes, pages: list, scale: float = 1.6, password: Optional[str] = None) -> list:
    """[(page number, PIL image)] rendered with pypdfium2 (local, no network)."""
    import pypdfium2 as pdfium
    try:
        doc = pdfium.PdfDocument(io.BytesIO(data), password=password)
    except Exception as e:
        raise PdfError(f"could not open the PDF to draw its pages ({type(e).__name__})")
    try:
        out = []
        for n in pages:
            out.append((n, doc[n - 1].render(scale=scale).to_pil()))
        return out
    finally:
        doc.close()
