import re as _re
import uuid
from pathlib import Path

from ..request_context import get_current_user_id
from .pdf_render import _save_text_or_markdown_as_pdf
from .workspace import MAX_EDIT_BYTES, _common_resolve


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

    # PowerPoint / Word: built on real layouts and styles so doc_edit can later
    # change one slide or paragraph without regenerating the rest
    elif sfx in (".pptx", ".ppt", ".docx", ".doc"):
        if not content.strip():
            return ("error: refusing to create an empty document - provide the content "
                    "(markdown; slides split by '---' or a leading '# ').")
        if sfx in (".ppt", ".doc"):
            p = p.with_suffix(sfx + "x")
            unique_path_arg = p.name
        try:
            data, spec = doc_ops.create(p.name, content)
        except DocOpError as e:
            return f"error: {e}"
        p.write_bytes(data)
        kind = "PowerPoint presentation" if p.suffix == ".pptx" else "Word document"
        msg = f"Wrote {kind} to common space: {unique_path_arg}. [DOWNLOAD: {unique_path_arg}]"

    # Handle .pdf conversion if writing to a PDF file (via HTML DOM first)
    elif sfx == ".pdf":
        if not content.strip():
            return ("error: refusing to create an empty PDF - provide the document content "
                    "(markdown or an HTML document).")
        if not _save_text_or_markdown_as_pdf(p, content):
            if p.is_file():
                p.unlink()
            return ("error: the PDF could not be compiled (Chrome/Edge missing or the render "
                    "failed), so nothing was saved. Send the document as markdown instead of "
                    "HTML, or try again.")
        spec = content
        msg = f"Wrote compiled PDF document to common space: {unique_path_arg}. [DOWNLOAD: {unique_path_arg}]"

    else:
        p.write_text(content, encoding="utf-8")
        msg = f"Wrote {len(content)} chars to common space: {unique_path_arg}. [DOWNLOAD: {unique_path_arg}]"

    if doc_ops.is_doc(p.name) and p.is_file():
        from .. import doc_store
        from ..doc_ops.base import sha256
        doc_store.register(get_current_user_id(), p.name, p.suffix.lstrip(".").lower(),
                           source_spec=spec, sha256=sha256(p.read_bytes()))
    return msg
