"""Automated Pre-flight Document Quality Assurance Linters.

Runs validation checks on generated office documents before delivery to users:
- Excel (.xlsx): Formula error detection (#VALUE!, #REF!), width truncation.
- Word (.docx): Unfilled template brackets, raw markdown leaks, heading hierarchy.
- PowerPoint (.pptx): Text bounding box overflow, contrast violations, slide density.
"""

from pathlib import Path
from typing import Optional

from .docx_lint import lint_docx
from .pptx_lint import lint_pptx
from .xlsx_lint import lint_xlsx

__all__ = ["lint_document", "lint_docx", "lint_pptx", "lint_xlsx"]


def lint_document(data: bytes, filename: str) -> list[str]:
    """Runs the appropriate QA linter on the file data based on extension.
    Returns a list of warning/error strings.
    """
    ext = Path(filename).suffix.lower()
    if ext == ".xlsx":
        return lint_xlsx(data)
    elif ext == ".docx":
        return lint_docx(data)
    elif ext == ".pptx":
        return lint_pptx(data)
    return []
