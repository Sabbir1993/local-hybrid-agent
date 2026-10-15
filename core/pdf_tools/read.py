"""Reading a PDF: per-page text, outline (bookmarks), metadata, and which pages are scans; and tables -> Excel / markdown."""
import io
import re
from typing import Optional

from .common import MAX_TEXT_CHARS, PdfError, mask, open_reader, parse_pages


def info(data: bytes, password: Optional[str] = None) -> dict:
    """{pages, title, author, encrypted, outline: [(level, title, page)], scanned_pages: [..], text_pages: n}"""
    import pdfplumber
    reader = open_reader(data, password)
    meta = reader.metadata or {}
    outline = []

    def walk(items, level=0):
        for it in items:
            if isinstance(it, list):
                walk(it, level + 1)
                continue
            try:
                outline.append((level, str(it.title), reader.get_destination_page_number(it) + 1))
            except Exception:
                continue
    try:
        walk(reader.outline)
    except Exception:
        pass
    scanned, texty = [], 0
    with pdfplumber.open(io.BytesIO(data), password=password or None) as pdf:
        for i, page in enumerate(pdf.pages[:400], 1):
            txt = (page.extract_text() or "").strip()
            if len(txt) >= 20:
                texty += 1
            elif page.images or not txt:
                scanned.append(i)
    return {"pages": len(reader.pages), "title": str(meta.get("/Title") or ""), "author": str(meta.get("/Author") or ""),
            "encrypted": bool(reader.is_encrypted), "outline": outline[:80], "scanned_pages": scanned, "text_pages": texty}


def read(data: bytes, pages=None, password: Optional[str] = None, layout: bool = False, offset: int = 0) -> str:
    """Text of the chosen pages with page markers, capped per call (`offset` pages through a long selection)."""
    import pdfplumber
    meta = info(data, password)
    want = parse_pages(pages, meta["pages"])
    head = [f"PDF: {meta['pages']} page{'s' if meta['pages'] != 1 else ''}"
            + (f", title \"{meta['title']}\"" if meta["title"] else "") + (f", author {meta['author']}" if meta["author"] else "")]
    if meta["outline"]:
        head.append("Outline: " + "; ".join(f"{'  ' * lv}{t} (p{p})" for lv, t, p in meta["outline"][:25]))
    if meta["scanned_pages"]:
        sp = meta["scanned_pages"]
        head.append(f"{len(sp)} page(s) have no text layer (scanned images: {', '.join(map(str, sp[:12]))}"
                    f"{'...' if len(sp) > 12 else ''}): use pdf_ocr or pdf_see for them")
    out, used, next_at = [], 0, None
    with pdfplumber.open(io.BytesIO(data), password=password or None) as pdf:
        for idx, n in enumerate(want):
            if idx < offset:
                continue
            page = pdf.pages[n - 1]
            try:
                txt = page.extract_text(layout=True) if layout else page.extract_text()
            except Exception:
                txt = ""
            txt = re.sub(r"[ \t]+\n", "\n", (txt or "").strip())
            block = f"--- page {n} ---\n{txt or '(no text on this page)'}"
            if used + len(block) > MAX_TEXT_CHARS and out:
                next_at = idx
                break
            out.append(block)
            used += len(block)
    text = "\n".join(head) + "\n\n" + "\n\n".join(out)
    if next_at is not None:
        text += f"\n\n(more pages follow: call again with offset={next_at})"
    return mask(text)


def _clean_cell(c) -> str:
    return re.sub(r"\s+", " ", str(c)).strip() if c is not None else ""


def extract_tables(data: bytes, pages=None, password: Optional[str] = None) -> list:
    """[{page, index, rows}] for every table pdfplumber finds on the chosen pages (merged / empty rows dropped)."""
    import pdfplumber
    n_pages = len(open_reader(data, password).pages)
    want = parse_pages(pages, n_pages)
    found = []
    with pdfplumber.open(io.BytesIO(data), password=password or None) as pdf:
        for n in want:
            page = pdf.pages[n - 1]
            try:
                tables = page.extract_tables() or []
                if not tables:                                   # borderless tables: fall back to text alignment
                    tables = page.extract_tables({"vertical_strategy": "text", "horizontal_strategy": "text",
                                                  "min_words_vertical": 3, "min_words_horizontal": 2}) or []
            except Exception:
                tables = []
            for k, t in enumerate(tables, 1):
                rows = [[_clean_cell(c) for c in r] for r in t if r and any(_clean_cell(c) for c in r)]
                width = max((len(r) for r in rows), default=0)
                if len(rows) >= 2 and width >= 2:
                    found.append({"page": n, "index": k, "rows": [r + [""] * (width - len(r)) for r in rows]})
    return found


def tables_to_outline(tables: list) -> str:
    """A workbook outline (core.sheet_engine.build): one sheet per table, header detected, sheet named by page and number."""
    from ..sheet_engine.pseudo import with_header
    out, used = [], set()
    for t in tables[:20]:
        base = f"p{t['page']}-t{t['index']}"
        name, k = base, 2
        while name in used:
            name, k = f"{base}-{k}", k + 1
        used.add(name)
        rows = with_header(t["rows"])
        width = max(len(r) for r in rows)
        esc = lambda c: str(c).replace("|", "\\|").replace("\n", " ")      # noqa: E731
        out.append(f"## Sheet: {name}")
        out.append("| " + " | ".join(esc(h) or f"Column{i + 1}" for i, h in enumerate(rows[0])) + " |")
        out.append("|" + "---|" * width)
        out += ["| " + " | ".join(esc(c) for c in r) + " |" for r in rows[1:]]
        out.append("")
    return "\n".join(out)


def tables_markdown(tables: list, limit_chars: int = 9000) -> str:
    out = []
    for t in tables:
        rows = t["rows"]
        out.append(f"Table {t['index']} on page {t['page']} ({len(rows)} rows x {len(rows[0])} columns):")
        out.append("| " + " | ".join(rows[0]) + " |")
        out.append("|" + "---|" * len(rows[0]))
        out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        out.append("")
    text = "\n".join(out)
    return mask(text if len(text) <= limit_chars else text[:limit_chars].rsplit("\n", 1)[0] + "\n... (cut)")


def tables_to_xlsx(tables: list, title: str = "") -> bytes:
    from ..sheet_engine import build
    if not tables:
        raise PdfError("no tables found on those pages")
    try:
        return build.build(tables_to_outline(tables), title=title, formulas=False)
    except build.BuildError as e:
        raise PdfError(str(e))
