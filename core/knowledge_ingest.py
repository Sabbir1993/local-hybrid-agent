"""
core/knowledge_ingest.py - text extraction for the organizational knowledge base.

This module is extraction only: no PCI logic lives here.

PAN handling for the knowledge base is done by the caller
(routes/knowledge/helpers.py::_finish_ingest) using core/pan.py, the single
canonical detector -- the one used for chat input, model output and cloud
egress. It MASKS card numbers to their last 4 digits before the text is
chunked and embedded, rather than rejecting the upload: a document that
legitimately discusses card handling must still be ingestible, and masking
already achieves the goal that matters, which is that no card number ever
enters the shared vector index and therefore can never be retrieved into a
prompt or sent to a cloud lane.
"""

import re
from pathlib import Path

MAX_URL_BYTES = 2 * 1024 * 1024
URL_TIMEOUT_S = 15.0


def extract_pdf(path: Path) -> str:
    import pdfplumber
    parts = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ""
            if t.strip():
                parts.append(t)
    return "\n\n".join(parts)


def extract_docx(path: Path) -> str:
    import docx
    d = docx.Document(str(path))
    return "\n".join(p.text for p in d.paragraphs if p.text.strip())


def extract_pptx(path: Path) -> str:
    """Slide text, tables and speaker notes, one block per slide."""
    from .doc_ops import pptx_ops
    out = []
    for e in pptx_ops.inspect(path.read_bytes())["elements"]:
        if e["kind"] == "slide":
            out.append(f"\n\nSlide {e['addr'][1:]}:")
        elif e["kind"] not in ("picture", "group", "table") and e["text"] and not e["text"].endswith(" paragraphs"):
            out.append(("Notes: " if e["kind"] == "notes" else "") + e["text"].strip())
    return "\n".join(out).strip()


def extract_xlsx(path: Path) -> str:
    import openpyxl
    wb = openpyxl.load_workbook(str(path), data_only=True, read_only=True)
    parts = []
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            cells = [str(c) for c in row if c is not None]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def extract_xls(path: Path) -> str:
    import xlrd
    wb = xlrd.open_workbook(str(path))
    parts = []
    for sheet in wb.sheets():
        for r in range(sheet.nrows):
            cells = [str(c) for c in sheet.row_values(r) if c not in (None, "")]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def extract_csv(path: Path) -> str:
    import csv
    import io
    content = None
    for enc in ("utf-8-sig", "latin-1"):
        try:
            content = path.read_text(encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    if content is None:
        content = path.read_text(encoding="utf-8", errors="replace")

    reader = csv.reader(io.StringIO(content))
    parts = []
    for row in reader:
        cells = [str(c).strip() for c in row if c is not None and str(c).strip()]
        if cells:
            parts.append(" | ".join(cells))
    return "\n".join(parts)


def extract_pasted_text(text: str) -> str:
    return text.strip()


async def extract_url(url: str) -> str:
    """Fetch a URL and strip it down to plain text. SSRF-guarded by
    core/net_guard.py (DNS-resolved public addresses only, on every redirect)."""
    import asyncio
    from .net_guard import guarded_get
    # every redirect hop is re-validated and DNS-resolved (core/net_guard.py);
    # the old follow_redirects=True let a public URL bounce to 127.0.0.1
    resp = await asyncio.to_thread(guarded_get, url, URL_TIMEOUT_S,
                                   {"User-Agent": "local-agent-kb/1.0"}, MAX_URL_BYTES)
    if resp.status_code >= 400:
        raise ValueError(f"HTTP {resp.status_code} fetching {url}")
    html = resp.content.decode(resp.encoding or "utf-8", errors="ignore")
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


EXTRACTORS = {
    ".pdf": extract_pdf,
    ".docx": extract_docx,
    ".pptx": extract_pptx,
    ".xlsx": extract_xlsx,
    ".xls": extract_xls,
    ".csv": extract_csv,
}


def extract_file(path: Path) -> str:
    ext = path.suffix.lower()
    fn = EXTRACTORS.get(ext)
    if fn is None:
        raise ValueError(f"unsupported file type: {ext}")
    return fn(path)
