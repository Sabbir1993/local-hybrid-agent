"""One-time nudge from "write a script with an Office library" to the doc_create tool.

A small model asked for an Excel / Word / PowerPoint file often reaches for run_python with openpyxl, python-docx or
python-pptx: it may not even be installed, it costs the user an approval card, and the result cannot be edited
later with doc_edit. doc_create does the same job in one safe call. The system prompt already says so (and has no
room for more), so the loop says it at the moment it matters: the first run_python of a run that imports one of those
libraries while doc_create is on offer is not run, with the reason. It is said once per run, so a script that really
needs a library feature doc_create lacks is never blocked, only asked to reconsider.
"""

import re
from typing import Optional

_LIB = r"(?:openpyxl|xlsxwriter|docx|pptx|python_pptx|python_docx)"
_IMPORT_RX = re.compile(rf"(?m)^\s*(?:import|from)\s+{_LIB}\b")
_QUOTED_RX = re.compile(rf"""(?:import_module|__import__|find_spec)\(\s*['"]{_LIB}['"]|\[[^\]]*['"]{_LIB}['"]""")

NOTE = ("error: not run. This is an Office document, so use doc_create instead of a script: "
        "doc_create(file='name.xlsx|.docx|.pptx|.csv', content=<markdown; tables as markdown tables or CSV; slides "
        "split by '---'>). It needs no library, no approval card, and doc_edit can change the file later. "
        "Only if doc_create truly cannot do what is needed, call run_python again.")


_BUILT_IN_PIECES = (".pdf", ".docx", ".pptx", ".ppt")
WRITE_NOTE = ("error: not saved. A {ext} file is built with doc_create, not write_file: "
              "doc_create(file='{name}', content=<the whole content as markdown, tables as markdown tables>) for the first part, then "
              "doc_append(file='{name}', content=..., done=true on the last call). A short document fits in one doc_create.")


def write_note(name: str, args, offered_names, already: set) -> Optional[str]:
    """Chat: the first write_file of a run that targets a .pdf / .docx / .pptx while doc_create is on offer is not run; the reply says
    the exact call to make instead. Once per run, so a case that truly needs write_file is only asked to reconsider."""
    if name != "write_file" or "doc_create" not in offered_names or "write_redirect" in already:
        return None
    path = str((args or {}).get("path") or (args or {}).get("file") or (args or {}).get("filename") or "").strip()
    low = path.lower()
    if not low.endswith(_BUILT_IN_PIECES):
        return None
    already.add("write_redirect")
    return WRITE_NOTE.format(ext=low.rsplit(".", 1)[-1].upper(), name=path.replace("'", "")[:80])


def note(name: str, args, offered_names, already: set) -> Optional[str]:
    """The refusal text for this call, or None to let it run. `already` is the per-run set that makes it fire once."""
    if name != "run_python" or "doc_create" not in offered_names or "doc_redirect" in already:
        return None
    code = str((args or {}).get("code") or "")
    if _IMPORT_RX.search(code) or _QUOTED_RX.search(code):
        already.add("doc_redirect")
        return NOTE
    return None
