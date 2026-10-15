"""PDF tools for chat and agent mode (implementations in core/pdf_tools/). Files are the caller's common-space files; every
tool that changes something saves a NEW file and returns its [DOWNLOAD: name] link."""

_FILE = {"type": "string", "description": "the PDF's file name, exactly as shown in the attachment note or download link"}
_PAGES = {"type": "string", "description": "page selection like '1-3,7,last', 'odd', 'even' or 'all' (default: all)"}
_PASSWORD = {"type": "string", "description": "only for a password-protected PDF; used for this call and never stored"}

PDF_READ_SCHEMA = {"type": "function", "function": {
    "name": "pdf_read",
    "description": "Read an attached or generated PDF: its text page by page (with page markers), outline / bookmarks, page count, "
                   "metadata, and which pages are scanned images without a text layer. Long documents are paged: use `pages` "
                   "or `offset` to continue. Use pdf_tables for tables and pdf_see / pdf_ocr for scanned pages.",
    "parameters": {"type": "object", "properties": {
        "file": _FILE, "pages": _PAGES, "offset": {"type": "integer", "description": "continue a long read from this page index"},
        "layout": {"type": "boolean", "description": "keep the visual column layout of the text"}, "password": _PASSWORD},
        "required": ["file"]}}}

PDF_TABLES_SCHEMA = {"type": "function", "function": {
    "name": "pdf_tables",
    "description": "Extract the tables from a PDF. to='xlsx' saves an Excel workbook with one sheet per table (header row detected, "
                   "numbers kept as text unless clear); to='markdown' (default) returns them as markdown tables. Reports how many "
                   "tables were found per page.",
    "parameters": {"type": "object", "properties": {
        "file": _FILE, "pages": _PAGES, "to": {"type": "string", "enum": ["markdown", "xlsx"]}, "password": _PASSWORD},
        "required": ["file"]}}}

PDF_PAGES_SCHEMA = {"type": "function", "function": {
    "name": "pdf_pages",
    "description": "Change a PDF's pages and save the result as a NEW file (the original is untouched). ops is a list applied in "
                   "order: {op:'merge', files:['b.pdf','c.pdf']}, {op:'extract', pages:'1-3'}, {op:'delete', pages:'4'}, "
                   "{op:'reorder', order:'3,1,2'}, {op:'rotate', pages:'2', degrees:90}, {op:'add_page_numbers', format:'Page {n} of "
                   "{total}', position:'bottom-center'}, {op:'watermark', text:'DRAFT', opacity:0.18, pages:'all'}, "
                   "{op:'set_metadata', title, author}, {op:'split', ranges:'1-3;4-6'} or {op:'split', every_n:10} (several files).",
    "parameters": {"type": "object", "properties": {
        "file": _FILE, "ops": {"type": "array", "items": {"type": "object"}, "description": "the page operations, in order"},
        "password": _PASSWORD}, "required": ["file", "ops"]}}}


def _wrap(fn):
    from ..pdf_tools.common import PdfError

    def run(args: dict) -> str:
        try:
            return fn(args or {})
        except PdfError as e:
            return f"error: {e}"
        except Exception as e:
            return f"error: could not process the PDF ({type(e).__name__})"
    return run


def _file(args: dict) -> str:
    from ..doc_tools.location import _file_arg
    try:
        return _file_arg(args)
    except ValueError:
        return ""


def _read(args: dict) -> str:
    from ..pdf_tools import read
    from ..pdf_tools.common import PdfError, resolve
    name = _file(args)
    if not name:
        raise PdfError("file required (the PDF's name)")
    p = resolve(name)
    return read.read(p.read_bytes(), args.get("pages"), args.get("password"), bool(args.get("layout")), int(args.get("offset") or 0))


def _tables(args: dict) -> str:
    from ..pdf_tools import read
    from ..pdf_tools.common import PdfError, resolve, save_new
    name = _file(args)
    if not name:
        raise PdfError("file required (the PDF's name)")
    p = resolve(name)
    data = p.read_bytes()
    found = read.extract_tables(data, args.get("pages"), args.get("password"))
    if not found:
        return ("No tables were found on those pages. If the pages are scanned images, run pdf_ocr first; "
                "otherwise the data may be laid out as plain text: read it with pdf_read.")
    per_page = {}
    for t in found:
        per_page[t["page"]] = per_page.get(t["page"], 0) + 1
    summary = f"Found {len(found)} table(s): " + ", ".join(f"page {k}: {v}" for k, v in sorted(per_page.items())) + "."
    if str(args.get("to") or "markdown").lower() == "xlsx":
        out = save_new(read.tables_to_xlsx(found, p.stem), p.name, "tables", ".xlsx")
        return f"{summary} Saved as {out}. [DOWNLOAD: {out}]"
    return summary + "\n\n" + read.tables_markdown(found)


def _pages(args: dict) -> str:
    from ..pdf_tools import pages as pg
    from ..pdf_tools.common import PdfError, resolve, save_new
    name = _file(args)
    if not name:
        raise PdfError("file required (the PDF's name)")
    p = resolve(name)
    files = [(p.name, p.read_bytes())]
    for op in args.get("ops") or []:
        if isinstance(op, dict) and str(op.get("op")).lower() == "merge":
            for other in (op.get("files") or ([op["file"]] if op.get("file") else [])):
                q = resolve(str(other))
                if all(q.name != f[0] for f in files):
                    files.append((q.name, q.read_bytes()))
    # merge refers to the names the caller wrote; map them to the resolved names
    ops = []
    for op in args.get("ops") or []:
        if isinstance(op, dict) and str(op.get("op")).lower() == "merge":
            op = dict(op)
            op["files"] = [resolve(str(o)).name for o in (op.get("files") or ([op["file"]] if op.get("file") else []))]
        ops.append(op)
    outputs, changes = pg.run(files, ops, args.get("password"))
    names = [save_new(data, p.name, tag) for data, tag in outputs]
    links = " ".join(f"[DOWNLOAD: {n}]" for n in names)
    return f"{'; '.join(changes)}. Saved as {', '.join(names)}. {links}"


PDF_IMPLS = {"pdf_read": _wrap(_read), "pdf_tables": _wrap(_tables), "pdf_pages": _wrap(_pages)}
PDF_SCHEMAS = [PDF_READ_SCHEMA, PDF_TABLES_SCHEMA, PDF_PAGES_SCHEMA]
