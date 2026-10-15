"""Password protection (AES-256) and removal, with the password given for this call only: it is never stored or logged."""
import io
from typing import Optional

from .common import PdfError, open_reader


def protect(data: bytes, user_password: str, owner_password: Optional[str] = None, allow_print: bool = True,
            allow_copy: bool = False, allow_modify: bool = False, password: Optional[str] = None) -> bytes:
    from pypdf import PdfWriter
    from pypdf.constants import UserAccessPermissions as P
    if not user_password or len(str(user_password)) < 6:
        raise PdfError("user_password required (at least 6 characters)")
    reader = open_reader(data, password)
    w = PdfWriter(clone_from=reader)
    flags = P(0)
    if allow_print:
        flags |= P.PRINT | P.PRINT_TO_REPRESENTATION
    if allow_copy:
        flags |= P.EXTRACT | P.EXTRACT_TEXT_AND_GRAPHICS
    if allow_modify:
        flags |= P.MODIFY | P.ADD_OR_MODIFY | P.ASSEMBLE_DOC | P.FILL_FORM_FIELDS
    w.encrypt(str(user_password), str(owner_password or user_password + "-owner"), permissions_flag=flags, algorithm="AES-256")
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def unprotect(data: bytes, password: str) -> bytes:
    from pypdf import PdfWriter
    reader = open_reader(data, password)
    if not reader.is_encrypted:
        raise PdfError("this PDF is not password protected")
    w = PdfWriter(clone_from=reader)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()
