"""
core/knowledge_ingest.py - text extraction for the organizational knowledge base.

PCI-DSS note: this runtime is not the cardholder-data environment, but as
security-adjacent internal tooling it must not let payment card numbers slip
into an internally-shared RAG index. _reject_pan() runs on every extracted
text before it reaches the caller -- ingestion fails closed (rejected, not
silently scrubbed) so the uploading admin sees exactly what to fix.
"""

import re
from pathlib import Path
from typing import Optional


_PAN_CANDIDATE = re.compile(r"(?:\d[ -]?){13,19}")
MAX_URL_BYTES = 2 * 1024 * 1024
URL_TIMEOUT_S = 15.0


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def find_pan_like(text: str) -> Optional[str]:
    """First Luhn-valid 13-19 digit run, or None. Card-shaped junk (random
    digit runs that fail Luhn) is left alone -- only real-looking PANs block."""
    for m in _PAN_CANDIDATE.finditer(text):
        digits = re.sub(r"[ -]", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return digits[:6] + "…" + digits[-4:]
    return None


def reject_if_pan(text: str) -> None:
    """Disabled: knowledge uploads are not blocked for payment card numbers."""
    pass


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
