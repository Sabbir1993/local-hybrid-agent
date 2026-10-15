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


PDF_FORM_SCHEMA = {"type": "function", "function": {
    "name": "pdf_form",
    "description": "Fill in a PDF form. Call with describe=true first to list the fields (names, types, options). Then pass "
                   "values={field name: value} to fill them (saved as a NEW file; flatten=true makes the answers permanent). "
                   "Only fill what the user gave you: never invent values.",
    "parameters": {"type": "object", "properties": {
        "file": _FILE, "describe": {"type": "boolean", "description": "list the form fields instead of filling"},
        "values": {"type": "object", "description": "field name -> value"}, "flatten": {"type": "boolean"}, "password": _PASSWORD},
        "required": ["file"]}}}

PDF_REDACT_SCHEMA = {"type": "function", "function": {
    "name": "pdf_redact",
    "description": "Permanently remove text from a PDF (true redaction, verified: the text can no longer be extracted). Give terms "
                   "(exact words, any case), patterns (regular expressions) and/or pan=true for payment card numbers. Pages with a "
                   "match become pictures. Saved as a NEW file. If nothing matches, nothing is saved.",
    "parameters": {"type": "object", "properties": {
        "file": _FILE, "terms": {"type": "array", "items": {"type": "string"}}, "patterns": {"type": "array", "items": {"type": "string"}},
        "pan": {"type": "boolean", "description": "also redact payment card numbers (Luhn-valid 13-19 digits)"},
        "pages": _PAGES, "password": _PASSWORD}, "required": ["file"]}}}

PDF_PROTECT_SCHEMA = {"type": "function", "function": {
    "name": "pdf_protect",
    "description": "Password-protect a PDF (AES-256) or remove the password. action='protect' needs user_password (6+ characters); "
                   "allow_print / allow_copy / allow_modify set what a reader may do. action='unprotect' needs the current password. "
                   "Saved as a NEW file. The passwords are never stored.",
    "parameters": {"type": "object", "properties": {
        "file": _FILE, "action": {"type": "string", "enum": ["protect", "unprotect"]},
        "user_password": {"type": "string"}, "owner_password": {"type": "string"},
        "allow_print": {"type": "boolean"}, "allow_copy": {"type": "boolean"}, "allow_modify": {"type": "boolean"},
        "password": {"type": "string", "description": "the current password (to open an already protected file / for unprotect)"}},
        "required": ["file", "action"]}}}


def _form(args: dict) -> str:
    from ..pdf_tools import forms
    from ..pdf_tools.common import PdfError, resolve, save_new
    name = _file(args)
    if not name:
        raise PdfError("file required (the PDF's name)")
    p = resolve(name)
    data = p.read_bytes()
    if args.get("describe") or not args.get("values"):
        return forms.describe(data, args.get("password"))
    out, filled = forms.fill(data, args["values"], bool(args.get("flatten")), args.get("password"))
    new = save_new(out, p.name, "filled")
    return f"Filled {len(filled)} field(s): {'; '.join(filled)}. Saved as {new}. [DOWNLOAD: {new}]"


def _redact(args: dict) -> str:
    from ..pdf_tools import redact
    from ..pdf_tools.common import PdfError, resolve, save_new
    name = _file(args)
    if not name:
        raise PdfError("file required (the PDF's name)")
    p = resolve(name)
    out, report = redact.redact(p.read_bytes(), args.get("terms"), args.get("patterns"), bool(args.get("pan")),
                                args.get("pages"), args.get("password"))
    new = save_new(out, p.name, "redacted")
    return f"{report} Saved as {new}. [DOWNLOAD: {new}]"


def _protect(args: dict) -> str:
    from ..pdf_tools import protect
    from ..pdf_tools.common import PdfError, resolve, save_new
    name = _file(args)
    if not name:
        raise PdfError("file required (the PDF's name)")
    p = resolve(name)
    action = str(args.get("action") or "protect").lower()
    if action == "unprotect":
        out = protect.unprotect(p.read_bytes(), str(args.get("password") or args.get("user_password") or ""))
        tag, msg = "unlocked", "Password removed."
    elif action == "protect":
        out = protect.protect(p.read_bytes(), args.get("user_password"), args.get("owner_password"),
                              args.get("allow_print", True) is not False, bool(args.get("allow_copy")),
                              bool(args.get("allow_modify")), args.get("password"))
        tag, msg = "protected", "Protected with AES-256."
    else:
        raise PdfError("action must be protect or unprotect")
    new = save_new(out, p.name, tag)
    return f"{msg} Saved as {new}. [DOWNLOAD: {new}]"


PDF_SEE_SCHEMA = {"type": "function", "function": {
    "name": "pdf_see",
    "description": "Look at PDF pages with the vision model (charts, layout, stamps, scanned pages, anything the text does not "
                   "show). Pages are drawn locally and only a local vision model is used. At most 4 pages per call. Give a question "
                   "for what you want to know.",
    "parameters": {"type": "object", "properties": {
        "file": _FILE, "pages": _PAGES, "question": {"type": "string"}, "password": _PASSWORD}, "required": ["file"]}}}

PDF_OCR_SCHEMA = {"type": "function", "function": {
    "name": "pdf_ocr",
    "description": "Recognise the text of scanned PDF pages. With Tesseract installed it saves a searchable copy of the PDF (the "
                   "scanned pages get a hidden text layer) and returns the text; without it the vision model transcribes the pages "
                   "(text only). Default pages: the ones pdf_read says have no text layer. lang like 'eng' or 'eng+ben'.",
    "parameters": {"type": "object", "properties": {
        "file": _FILE, "pages": _PAGES, "lang": {"type": "string"}, "password": _PASSWORD}, "required": ["file"]}}}


def _wrap_async(fn):
    from ..pdf_tools.common import PdfError

    async def run(args: dict) -> str:
        try:
            return await fn(args or {})
        except PdfError as e:
            return f"error: {e}"
        except Exception as e:
            return f"error: could not process the PDF ({type(e).__name__})"
    return run


async def _see(args: dict) -> str:
    from ..pdf_tools import see
    from ..pdf_tools.common import PdfError, resolve
    name = _file(args)
    if not name:
        raise PdfError("file required (the PDF's name)")
    p = resolve(name)
    return await see.see(p.read_bytes(), args.get("pages"), str(args.get("question") or ""), args.get("password"))


async def _ocr(args: dict) -> str:
    import asyncio
    from ..pdf_tools import ocr
    from ..pdf_tools.common import PdfError, resolve, save_new
    name = _file(args)
    if not name:
        raise PdfError("file required (the PDF's name)")
    if not ocr.enabled():
        raise PdfError("OCR is switched off (chat.pdf_ocr)")
    p = resolve(name)
    data = p.read_bytes()
    pages = ocr.selected_pages(data, args.get("pages"), args.get("password"))
    exe = ocr.find_tesseract()
    if exe:
        lang = ocr.check_lang(args.get("lang"), ocr.languages(exe))
        out, texts = await asyncio.to_thread(ocr.ocr_with_tesseract, exe, data, pages, lang, args.get("password"))
        new = save_new(out, p.name, "searchable")
        return (f"Recognised {len(texts)} page(s) with Tesseract ({lang}). Saved a searchable copy as {new}. [DOWNLOAD: {new}]\n\n"
                + ocr.render_text(texts))
    try:
        texts = await ocr.ocr_with_vision(data, pages, args.get("password"))
    except PdfError:
        raise PdfError(ocr.how_to_enable())
    return ("Tesseract is not installed, so the vision model transcribed the pages (text only, no searchable copy; check numbers "
            "and names against the original).\n\n" + ocr.render_text(texts))


PDF_IMPLS = {"pdf_see": _wrap_async(_see), "pdf_ocr": _wrap_async(_ocr), "pdf_read": _wrap(_read), "pdf_tables": _wrap(_tables), "pdf_pages": _wrap(_pages), "pdf_form": _wrap(_form),
             "pdf_redact": _wrap(_redact), "pdf_protect": _wrap(_protect)}
PDF_SCHEMAS = [PDF_SEE_SCHEMA, PDF_OCR_SCHEMA, PDF_READ_SCHEMA, PDF_TABLES_SCHEMA, PDF_PAGES_SCHEMA, PDF_FORM_SCHEMA, PDF_REDACT_SCHEMA, PDF_PROTECT_SCHEMA]
