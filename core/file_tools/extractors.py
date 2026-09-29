import csv
from pathlib import Path
from .constants import DEFAULT_MAX_CHARS


def _extract_excel(path: Path) -> str:
    """Extract cell data from .xlsx / .xls workbooks via openpyxl or xlrd."""
    suffix = path.suffix.lower()
    parts = []
    if suffix == ".xls":
        try:
            import xlrd
            wb = xlrd.open_workbook(str(path))
            for sheet in wb.sheets():
                parts.append(f"## Sheet: {sheet.name}")
                for row_idx in range(sheet.nrows):
                    row = [str(sheet.cell_value(row_idx, c)) for c in range(sheet.ncols)]
                    parts.append("\t".join(row))
        except ImportError:
            parts.append("(xlrd not installed - install with: pip install xlrd)")
        except Exception as e:
            parts.append(f"(error reading .xls: {e})")
    else:
        try:
            import openpyxl
            wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                parts.append(f"## Sheet: {sheet_name}")
                row_count = 0
                for row in ws.iter_rows(values_only=True):
                    row_strs = [str(c) if c is not None else "" for c in row]
                    if any(v.strip() for v in row_strs):   # skip blank rows
                        parts.append("\t".join(row_strs))
                        row_count += 1
                        if row_count >= 2000:     # cap at 2000 rows per sheet
                            parts.append(f"... (truncated at 2000 rows)")
                            break
            wb.close()
        except ImportError:
            parts.append("(openpyxl not installed - install with: pip install openpyxl)")
        except Exception as e:
            parts.append(f"(error reading Excel: {e})")
    return "\n".join(parts)


def _extract_csv(path: Path) -> str:
    """Extract all rows from a CSV file."""
    parts = []
    try:
        with path.open(encoding="utf-8", errors="replace", newline="") as f:
            reader = csv.reader(f)
            row_count = 0
            for row in reader:
                parts.append("\t".join(row))
                row_count += 1
                if row_count >= 3000:
                    parts.append("... (truncated at 3000 rows)")
                    break
    except Exception as e:
        parts.append(f"(error reading CSV: {e})")
    return "\n".join(parts)


def _extract_pdf(path: Path) -> str:
    """Extract text and tables from a PDF via pdfplumber."""
    parts = []
    try:
        import pdfplumber
        with pdfplumber.open(str(path)) as pdf:
            page_count = len(pdf.pages)
            parts.append(f"(PDF - {page_count} page{'s' if page_count != 1 else ''})")
            for i, page in enumerate(pdf.pages[:100]):   # cap at 100 pages
                page_num = i + 1
                text = (page.extract_text() or "").strip()
                tables = page.extract_tables() or []
                if text or tables:
                    parts.append(f"\n--- Page {page_num} ---")
                if text:
                    parts.append(text)
                for tbl in tables:
                    parts.append("")
                    for row in tbl:
                        row_strs = [str(c) if c is not None else "" for c in row]
                        parts.append("\t".join(row_strs))
            if page_count > 100:
                parts.append(f"\n... ({page_count - 100} more pages - use read_file_chunk to access them)")
    except ImportError:
        parts.append("(pdfplumber not installed - install with: pip install pdfplumber)")
    except Exception as e:
        parts.append(f"(error reading PDF: {e})")
    return "\n".join(parts)


def _extract_office(path: Path) -> str:
    """PowerPoint / Word via core.doc_ops: the same addressed outline doc_edit
    targets (slides, shapes, notes, tables; paragraphs, tables, headers)."""
    from .. import doc_ops
    if path.suffix.lower() in (".ppt", ".doc"):
        return (f"(legacy {path.suffix} format - text cannot be read; open it in Office and save as "
                f"{path.suffix}x, then attach it again)")
    try:
        return doc_ops.inspect(path.read_bytes(), path.name, max_chars=10 ** 9)
    except Exception as e:
        return f"(error reading {path.suffix} document: {e})"


_extract_pptx = _extract_docx = _extract_office


def _extract_text(path: Path) -> str:
    """Read plain text files (json, xml, txt, md, etc.)."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"(error reading file: {e})"


def extract_file_content(path, max_chars: int = DEFAULT_MAX_CHARS):
    """Extract text content from a document file.

    Returns:
        (content: str, truncated: bool)
        content is the extracted text, truncated if it exceeds max_chars.
    """
    p = Path(path)
    if not p.is_file():
        return f"(file not found: {path})", False

    suffix = p.suffix.lower()
    if suffix in (".xlsx", ".xls"):
        text = _extract_excel(p)
    elif suffix == ".csv":
        text = _extract_csv(p)
    elif suffix == ".pdf":
        text = _extract_pdf(p)
    elif suffix in (".pptx", ".ppt"):
        text = _extract_pptx(p)
    elif suffix in (".docx", ".doc"):
        text = _extract_docx(p)
    elif suffix in (".png", ".jpg", ".jpeg", ".webp"):
        text = f"[IMAGE ATTACHMENT: {p.name}]\nWorkspace path: {p.name}\n(Perceptual pre-processing ready: Call analyze_image for grounded OCR coordinates or scene summary.)"
    else:
        # json, xml, txt, md, etc. - raw text
        text = _extract_text(p)

    if len(text) > max_chars:
        return text[:max_chars], True
    return text, False
