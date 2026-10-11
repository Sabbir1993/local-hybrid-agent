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


def note(name: str, args, offered_names, already: set) -> Optional[str]:
    """The refusal text for this call, or None to let it run. `already` is the per-run set that makes it fire once."""
    if name != "run_python" or "doc_create" not in offered_names or "doc_redirect" in already:
        return None
    code = str((args or {}).get("code") or "")
    if _IMPORT_RX.search(code) or _QUOTED_RX.search(code):
        already.add("doc_redirect")
        return NOTE
    return None
