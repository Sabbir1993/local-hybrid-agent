"""core/doc_ops - read, create and surgically edit office documents.

    inspect(data, name, focus=None)       -> outline text with stable addresses
    edit(data, name, ops, source_spec)    -> EditOutcome (new bytes + change list)
    create(name, content, template=None)  -> (bytes, source_spec)

Everything works on bytes in memory, so the same code serves chat mode (the
user's common space on the server) and agent mode (bytes fetched from and
written back to the user's device through the companion; never stored here).

"Only the asked part changes" is enforced, not hoped for: see base.py.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import csv_ops, docx_ops, md_ops, pptx_ops, xlsx_ops
from .base import MACRO_EXTS, DocOpError, check_size, require

EDITABLE = {".pptx": pptx_ops, ".xlsx": xlsx_ops, ".csv": csv_ops, ".docx": docx_ops, ".md": md_ops}
LEGACY = {".ppt": ".pptx", ".xls": ".xlsx", ".doc": ".docx"}
DOC_EXTS = set(EDITABLE) | {".pdf"}
DEFAULT_MAX_CHARS = 12_000

__all__ = ["DocOpError", "EditOutcome", "inspect", "edit", "create", "DOC_EXTS", "is_doc"]


@dataclass
class EditOutcome:
    data: bytes
    changes: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    source_spec: Optional[str] = None


def _ext(name: str) -> str:
    return Path(str(name)).suffix.lower()


def is_doc(name: str) -> bool:
    return _ext(name) in DOC_EXTS


def _guard_ext(ext: str) -> None:
    if ext in MACRO_EXTS:
        raise DocOpError("macro-enabled Office files are not supported; save it as .pptx/.xlsx/.docx first")
    if ext in LEGACY:
        raise DocOpError(f"legacy {ext} files cannot be edited; open it and save as {LEGACY[ext]} first")


# ------------------------------------------------------------------
# PAN (PCI DSS) guards
# ------------------------------------------------------------------

def _pan():
    from .. import pan
    return pan


def _mask(text: str) -> str:
    p = _pan()
    return p.mask_pans(text)[0] if p.enabled("pan_output") else text


def _check_ops_for_pan(ops) -> None:
    p = _pan()
    visible = [{k: v for k, v in o.items() if not str(k).startswith("_")} for o in ops]  # not image bytes
    if p.enabled("pan_input") and p.contains_pan(json.dumps(visible, ensure_ascii=False, default=str)):
        raise DocOpError("the edit contains a payment card number; card numbers must not be written "
                         "into documents (PCI DSS). Use a masked value such as ****1234.")


def _pan_count(text: str) -> int:
    p = _pan()
    return sum(1 for m in p.PAN_RX.finditer(text or "") if p.is_pan(m.group(0)))


# ------------------------------------------------------------------
# Reading
# ------------------------------------------------------------------

def _outline(data: bytes, ext: str, source_spec: Optional[str]) -> dict:
    if ext == ".pdf":
        if source_spec:
            o = md_ops.inspect_text(source_spec)
            o["header"] = "PDF generated from an editable source. " + o["header"]
            return o
        return _pdf_outline(data)
    return EDITABLE[ext].inspect(data)


def _pdf_outline(data: bytes) -> dict:
    import io
    import pdfplumber
    els = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        n = len(pdf.pages)
        for i, page in enumerate(pdf.pages[:100], 1):
            t = (page.extract_text() or "").strip()
            if t:
                els.append({"addr": f"page{i}", "kind": "page", "text": t[:1500]})
    return {"kind": "pdf", "header": f"PDF: {n} pages (uploaded PDF - read-only; ask for a DOCX version to edit)",
            "elements": els}


def render(outline: dict, focus: Optional[str] = None, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    els = outline["elements"]
    if focus:
        f = str(focus).strip().lower()
        hit = [i for i, e in enumerate(els) if e["addr"].lower() == f or e["addr"].lower().startswith(f.rstrip("/") + "/")
               or e["addr"].lower().startswith(f)]
        if hit:
            lo, hi = max(hit[0] - 5, 0), min(hit[-1] + 6, len(els))
            els = els[lo:hi]
    lines = [outline["header"], "Addresses in [brackets] are what doc_edit ops target."]
    for e in els:
        lines.append(f"[{e['addr']}] ({e['kind']}) {e['text']}")
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n... (truncated - call doc_inspect with focus=<address> to see more)"
    return text


def inspect(data: bytes, name: str, focus: Optional[str] = None, max_chars: int = DEFAULT_MAX_CHARS,
            source_spec: Optional[str] = None) -> str:
    ext = _ext(name)
    _guard_ext(ext)
    if ext not in DOC_EXTS:
        raise DocOpError(f"{ext or 'this file type'} is not a supported document (have: {', '.join(sorted(DOC_EXTS))})")
    check_size(data)
    return _mask(render(_outline(data, ext, source_spec), focus, max_chars))


def full_text(data: bytes, name: str, source_spec: Optional[str] = None) -> str:
    """Whole outline (no truncation) - used for PAN scans and KB ingest."""
    return render(_outline(data, _ext(name), source_spec), None, 10 ** 9)


# ------------------------------------------------------------------
# Editing
# ------------------------------------------------------------------

def normalize_ops(ops) -> list[dict]:
    if isinstance(ops, str):
        try:
            ops = json.loads(ops)
        except json.JSONDecodeError:
            raise DocOpError("ops must be a JSON list of {op, addr, ...} objects")
    if isinstance(ops, dict):
        ops = [ops]
    if not isinstance(ops, list) or not ops or not all(isinstance(o, dict) and o.get("op") for o in ops):
        raise DocOpError("ops must be a non-empty list of objects, each with an 'op' field")
    if len(ops) > 200:
        raise DocOpError("too many ops in one edit (max 200)")
    return ops


def edit(data: bytes, name: str, ops, source_spec: Optional[str] = None) -> EditOutcome:
    ext = _ext(name)
    _guard_ext(ext)
    check_size(data)
    ops = normalize_ops(ops)
    _check_ops_for_pan(ops)
    if ext == ".pdf":
        if not source_spec:
            raise DocOpError("this PDF was not generated here, so it has no editable source. Ask for it to be "
                             "converted to DOCX (doc_create from its text), then edit the DOCX.")
        new_md, changes = md_ops.apply_text(source_spec, ops)
        if _pan_count(new_md) > _pan_count(source_spec):
            raise DocOpError("the edited document would contain a new payment card number (PCI DSS)")
        return EditOutcome(render_pdf(new_md, Path(name).stem), changes, ["PDF re-rendered from its source"], new_md)
    mod = EDITABLE.get(ext)
    if mod is None:
        raise DocOpError(f"{ext} files cannot be edited (have: {', '.join(sorted(DOC_EXTS))})")
    before = full_text(data, name)
    res = mod.apply(data, ops)
    if _pan_count(full_text(res.data, name)) > _pan_count(before):
        raise DocOpError("the edited document would contain a new payment card number (PCI DSS)")
    spec = res.data.decode("utf-8") if ext == ".md" else None
    return EditOutcome(res.data, res.changes, res.notes, spec)


# ------------------------------------------------------------------
# Creation
# ------------------------------------------------------------------

def _rows_from_text(content: str) -> list[list]:
    """Rows for a new spreadsheet. _parse_tabular_text already returns one column
    of lines when nothing multi-column exists, or [] when the content is code -
    so its answer is authoritative: overriding it here used to put prose and code
    back into the sheet."""
    from ..agent_tools import _parse_tabular_text
    return _parse_tabular_text(content)


def render_pdf(md: str, title: str = "document") -> bytes:
    """Markdown -> PDF bytes via the existing HTML/Chromium (ReportLab fallback) renderer.
    The renderer needs a file path; a temp file is used and removed immediately."""
    import os
    import tempfile
    from ..agent_tools import _save_text_or_markdown_as_pdf
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", title)[:60] or "document"
    d = tempfile.mkdtemp(prefix="docops_")
    p = Path(d) / f"{safe}.pdf"
    try:
        if not _save_text_or_markdown_as_pdf(p, md) or not p.is_file():
            raise DocOpError("PDF rendering failed")
        return p.read_bytes()
    finally:
        try:
            if p.exists():
                p.unlink()
            os.rmdir(d)
        except OSError:
            pass


def create(name: str, content: str, template: Optional[bytes] = None) -> tuple[bytes, Optional[str]]:
    """(file bytes, source_spec) for a new document built from markdown / table text."""
    ext = _ext(name)
    _guard_ext(ext)
    _check_ops_for_pan([{"op": "create", "content": content}])
    title = Path(name).stem.replace("_", " ").replace("-", " ").title()
    if ext == ".pptx":
        return pptx_ops.create(content, template=template, title=title), content
    if ext == ".docx":
        return docx_ops.create(content, title=title), content
    if ext == ".xlsx":
        rows = _rows_from_text(content)
        require(rows, "no tabular data found in the content - a spreadsheet needs rows "
                      "(a markdown table or delimited rows), not prose or code")
        return xlsx_ops.create(rows), content
    if ext == ".csv":
        rows = _rows_from_text(content)
        require(rows, "no tabular data found in the content - a CSV needs rows "
                      "(a markdown table or delimited rows), not prose or code")
        return csv_ops.create(rows), None
    if ext == ".pdf":
        return render_pdf(content, title), content
    raise DocOpError(f"cannot create {ext} documents")
