"""The verify loop: a syntax check after every write, with a bounded number of fix attempts.

The result is a short text appended to the tool result. It never contains file content beyond
one error line. After `agent.verify_max_retries` failed checks in a row on one file the model is
told to stop; when the file used to pass and the change was an edit, the last passing version
is restored automatically.
"""
import sys
from pathlib import Path
from typing import Optional

from .. import companion_bridge
from .verify import COMPANION_CHECKED, verify_text
from . import file_state
from .limits import agent_limit


def _cb():
    return getattr(sys.modules.get("core.agent_tools"), "companion_bridge", companion_bridge)


async def _check(uid: int, p: Path, rel: str, text: str) -> tuple[str, str]:
    status, detail = verify_text(rel, text)
    if status == "skip" and p.suffix.lower() in COMPANION_CHECKED:
        try:
            data = await _cb().call(uid, "fs.verify", {"path": str(p)}, timeout=20)
            if data.get("checked"):
                return ("ok", "javascript syntax") if data.get("ok") else \
                    ("fail", " ".join(str(data.get("detail") or "syntax error").split())[:240])
        except Exception:
            pass                          # an older companion has no fs.verify: no check, no error
    return status, detail


async def verify_after(uid: int, p: Path, rel: str, text: str, before: Optional[str],
                       can_autorevert: bool) -> str:
    status, detail = await _check(uid, p, rel, text)
    if status == "skip":
        return ""
    st = file_state.verify_state(str(p))
    if st["good"] is None and before:
        if verify_text(rel, before)[0] == "ok":
            st["good"] = before           # the file passed before this change: a restore point
    if status == "ok":
        st["fails"], st["good"] = 0, text
        return f"\nverify: OK ({detail})"

    st["fails"] += 1
    limit = agent_limit("verify_max_retries")
    if st["fails"] >= limit:
        if can_autorevert and st["good"] is not None and st["good"] != text:
            good = st["good"]
            await _cb().call(uid, "fs.write", {"path": str(p), "content": good, "append": False})
            file_state.push_undo(str(p), text)
            st["fails"] = 0
            return (f"\nverify: FAILED - {detail}. {limit} failed attempts in a row, so {rel} was restored "
                    "to the last version that passed. Stop editing this file and tell the user what you "
                    "were trying to change and why it failed.")
        return (f"\nverify: FAILED - {detail}. {limit} failed attempts in a row on {rel}. Stop and tell the "
                "user what is wrong instead of guessing again.")
    hint = ""
    if not can_autorevert:
        hint = " If this is only the first part of a larger file, continue - it is checked again after each chunk."
    return (f"\nverify: FAILED - {detail}. Fix it before moving on (read_file the lines near the error). "
            f"Attempt {st['fails']}/{limit}.{hint}")
