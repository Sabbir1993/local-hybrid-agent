"""A finished .pptx / .xlsx / .docx as a PDF, through LibreOffice when it is installed (Linux and Windows).

Used only to LOOK at the document (core/doc_tools/look.py): the pages are rendered to pictures for the local vision model.
The file the user downloads is never replaced by LibreOffice's copy. Not installed, disabled, slow or failing -> None, and
the document is simply not looked at."""
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from ..sheet_engine.office import find_soffice

TIMEOUT_S = 90
RENDERABLE = (".pptx", ".xlsx", ".docx")


def enabled() -> bool:
    try:
        from ..small_model import APP_CONFIG
        return (APP_CONFIG.get("chat") or {}).get("render_preview", True) is not False
    except Exception:
        return True


def to_pdf(data: bytes, ext: str) -> Optional[bytes]:
    ext = str(ext or "").lower()
    if ext not in RENDERABLE or not data or not enabled():
        return None
    exe = find_soffice()
    if not exe:
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="docrender_") as d:
            src, out = Path(d) / f"in{ext}", Path(d) / "out"
            src.write_bytes(data)
            out.mkdir()
            # a private profile dir: never touches (or waits on) the user's own LibreOffice session
            profile = Path(d) / "profile"
            cmd = [exe, f"-env:UserInstallation={profile.as_uri()}", "--headless", "--norestore", "--nolockcheck",
                   "--convert-to", "pdf", "--outdir", str(out), str(src)]
            subprocess.run(cmd, check=True, timeout=TIMEOUT_S, capture_output=True, stdin=subprocess.DEVNULL)
            done = out / "in.pdf"
            return done.read_bytes() if done.is_file() else None
    except Exception as e:
        print(f"[render] LibreOffice preview skipped ({type(e).__name__}: {e})", file=sys.stderr)
        return None
