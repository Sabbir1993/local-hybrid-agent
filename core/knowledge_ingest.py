"""
core/knowledge_ingest.py - text extraction for the organizational knowledge base.

PCI-DSS note: this runtime is not the cardholder-data environment, but as
security-adjacent internal tooling it must not let payment card numbers slip
into an internally-shared RAG index. _reject_pan() runs on every extracted
text before it reaches the caller -- ingestion fails closed (rejected, not
silently scrubbed) so the uploading admin sees exactly what to fix.
"""

import ipaddress
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx

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
    hit = find_pan_like(text)
    if hit:
        raise ValueError(
            f"Ingestion blocked: content looks like it contains a payment card "
            f"number ({hit}). Remove it and try again. Never store real PANs in "
            f"the knowledge base."
        )


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


def extract_pasted_text(text: str) -> str:
    return text.strip()


_BLOCKED_HOST_SUFFIXES = ("localhost",)


def _is_private_host(host: str) -> bool:
    if not host:
        return True
    h = host.lower()
    if any(h == s or h.endswith("." + s) for s in _BLOCKED_HOST_SUFFIXES):
        return True
    try:
        ip = ipaddress.ip_address(h)
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
    except ValueError:
        return False  # a hostname, not a literal IP -- DNS resolution isn't checked here


async def extract_url(url: str) -> str:
    """Fetch a URL and strip it down to plain text. Basic SSRF guard: refuses
    localhost / loopback / private-range literals up front (best-effort --
    this app has no outbound network policy layer to fully close DNS-rebinding)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("only http/https URLs are supported")
    if _is_private_host(parsed.hostname or ""):
        raise ValueError("refusing to fetch a private/loopback address")
    async with httpx.AsyncClient(timeout=URL_TIMEOUT_S, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": "local-agent-kb/1.0"})
        resp.raise_for_status()
        body = resp.content[:MAX_URL_BYTES]
    html = body.decode(resp.encoding or "utf-8", errors="ignore")
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
}


def extract_file(path: Path) -> str:
    ext = path.suffix.lower()
    fn = EXTRACTORS.get(ext)
    if fn is None:
        raise ValueError(f"unsupported file type: {ext}")
    return fn(path)
