import re as _re
import uuid
from pathlib import Path

from ..request_context import get_current_user_id
from .pdf_render import _save_text_or_markdown_as_pdf
from .workspace import MAX_EDIT_BYTES, _common_resolve


def _run_blocking(factory):
    """Run an async step from this sync tool. Called from a worker thread (the chat route does that) it just runs; called
    from inside a running event loop it runs on a helper thread with the same context, so the caller waits but nothing
    deadlocks on the loop it is standing on."""
    import asyncio
    import contextvars
    from concurrent.futures import ThreadPoolExecutor
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())
    ctx = contextvars.copy_context()
    with ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(ctx.run, asyncio.run, factory()).result()


def _prepare(ext: str, content: str) -> tuple:
    """(content, images, note) for a deck / PDF: the session's style and the pictures its `image:` lines ask for. Never
    raises: on any failure the document is written as it was."""
    try:
        from ..doc_tools import operations as ops
        return _run_blocking(lambda: ops.prepare_build(ext, content, get_current_user_id()))
    except Exception as e:
        import sys
        print(f"[write_file] picture / style step skipped ({type(e).__name__}: {e})", file=sys.stderr)
        return content, {}, ""


def _after_build(ext: str, data: bytes, content: str, extra: str) -> str:
    """Picture notes and the layout / PDF check, as one extra sentence for the tool result."""
    try:
        from ..doc_tools import operations as ops
        note = ops._layout_note(ext, data, content)
    except Exception:
        note = ""
    return (" " + extra if extra else "") + (" " + note if note else "")


def tool_write_file_common(args: dict) -> str:
    path_arg = args.get("path") or args.get("file") or args.get("filename")
    if not path_arg and args.get("content"):
        c_low = args["content"][:300].lower()
        if "<!doctype html" in c_low or "<html" in c_low:
            path_arg = "index.html"
        elif "def " in c_low or "import " in c_low:
            path_arg = "main.py"
        elif "|" in c_low:
            path_arg = "data.csv"
        else:
            path_arg = "output.txt"
    if not path_arg:
        raise ValueError("path required")

    clean_name = Path(path_arg).name
    stem = Path(clean_name).stem
    suffix = Path(clean_name).suffix or ".txt"

    clean_stem = _re.sub(r'([-_][0-9a-fA-F]{8})+$', '', stem)
    unique_id = uuid.uuid4().hex[:8]
    unique_path_arg = f"{clean_stem}-{unique_id}{suffix}"

    p = _common_resolve(unique_path_arg)
    p.parent.mkdir(parents=True, exist_ok=True)
    content = args.get("content", "")
    if len(content) > MAX_EDIT_BYTES:
        raise ValueError("content too large")

    from .. import doc_ops
    from ..doc_ops.base import DocOpError
    try:
        doc_ops._check_ops_for_pan([{"op": "write", "content": content}])
    except DocOpError as e:
        return f"error: write refused: {e}"
    sfx = suffix.lower()
    spec = None

    # Handle .xlsx / .xls conversion if structured text data is provided
    if sfx in (".xlsx", ".xls"):
        if not content.strip():
            return ("error: refusing to create an empty spreadsheet - provide the data rows. "
                    "A spreadsheet is never filled with placeholder rows.")
        if sfx == ".xls":
            p = p.with_suffix(".xlsx")
            unique_path_arg = p.name
        try:
            data, spec = doc_ops.create(p.name, content)
        except DocOpError as e:
            return f"error: {e}"
        p.write_bytes(data)
        msg = f"Wrote Excel file to common space: {unique_path_arg}. [DOWNLOAD: {unique_path_arg}]"
        msg += _sheet_note(data)

    # PowerPoint / Word: built on real layouts and styles so doc_edit can later
    # change one slide or paragraph without regenerating the rest
    elif sfx in (".pptx", ".ppt", ".docx", ".doc"):
        if not content.strip():
            return ("error: refusing to create an empty document - provide the content "
                    "(markdown; slides split by '---' or a leading '# ').")
        if sfx in (".ppt", ".doc"):
            p = p.with_suffix(sfx + "x")
            unique_path_arg = p.name
        extra = ""
        images: dict = {}
        try:
            if p.suffix == ".pptx":
                content, images, extra = _prepare(".pptx", content)
            data, spec = doc_ops.create(p.name, content, images=images or None)
        except DocOpError as e:
            return f"error: {e}"
        p.write_bytes(data)
        kind = "PowerPoint presentation" if p.suffix == ".pptx" else "Word document"
        msg = f"Wrote {kind} to common space: {unique_path_arg}. [DOWNLOAD: {unique_path_arg}]"
        msg += _after_build(p.suffix, data, content, extra)

    # Handle .pdf conversion if writing to a PDF file (via HTML DOM first)
    elif sfx == ".pdf":
        if not content.strip():
            return ("error: refusing to create an empty PDF - provide the document content "
                    "(markdown or an HTML document).")
        content, images, extra = _prepare(".pdf", content)
        if not _save_text_or_markdown_as_pdf(p, content, images=images or None):
            if p.is_file():
                p.unlink()
            return ("error: the PDF could not be compiled (Chrome/Edge missing or the render "
                    "failed), so nothing was saved. Send the document as markdown instead of "
                    "HTML, or try again.")
        spec = content
        msg = f"Wrote compiled PDF document to common space: {unique_path_arg}. [DOWNLOAD: {unique_path_arg}]"
        msg += _after_build(".pdf", p.read_bytes(), content, extra)

    else:
        p.write_text(content, encoding="utf-8")
        msg = f"Wrote {len(content)} chars to common space: {unique_path_arg}. [DOWNLOAD: {unique_path_arg}]"

    if doc_ops.is_doc(p.name) and p.is_file():
        from .. import doc_store
        from ..doc_ops.base import sha256
        doc_store.register(get_current_user_id(), p.name, p.suffix.lstrip(".").lower(),
                           source_spec=spec, sha256=sha256(p.read_bytes()))
    return msg


def _sheet_note(data: bytes) -> str:
    """What the built-in calculator found in a workbook with formulas ('' for a plain table or when the check fails)."""
    try:
        from ..sheet_engine import calc
        note = calc.check_report(calc.calculate(data))
        return (" " + note) if note else ""
    except Exception:
        return ""
