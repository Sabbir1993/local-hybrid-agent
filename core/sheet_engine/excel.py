"""Calculate a workbook with the real Microsoft Excel on the signed-in user's own PC, through their companion app (op `excel.recalc`,
companion/excelops.js). Used first by core.sheet_engine.calc when it is available; LibreOffice and the built-in calculator are the
fallbacks, so nothing changes for a user without the companion, without Excel, or who denies the question the companion asks.

The workbook travels server -> that user's companion -> back as numbers; Excel runs there with macros disabled on a temporary copy.
Nothing is retried (Excel may already be working on it) and every failure is silent: None means "use the next engine".
"""
import base64
import sys
import time
from typing import Optional

OP = "excel.recalc"
CAPS_TTL_S = 600
CALL_TIMEOUT_S = 75               # includes the one-time Allow / Deny question on the user's PC
_caps: dict = {}                  # user id -> (checked at, set of op names)


def enabled() -> bool:
    try:
        from ..small_model import APP_CONFIG
        return (APP_CONFIG.get("chat") or {}).get("sheet_excel", True) is not False
    except Exception:
        return True


def _bridge():
    from .. import companion_bridge
    return companion_bridge


def _uid() -> Optional[int]:
    from ..request_context import get_current_user_id
    return get_current_user_id()


def _run(factory):
    from ..agent_tools.common_writer import _run_blocking
    return _run_blocking(factory)


def capabilities(uid: int) -> set:
    """The ops this user's companion offers (cached; an old companion that does not know the question gives an empty set)."""
    hit = _caps.get(uid)
    if hit and time.time() - hit[0] < CAPS_TTL_S:
        return hit[1]
    ops: set = set()
    try:
        res = _run(lambda: _bridge().call(uid, "companion.capabilities", {}, timeout=8))
        ops = set((res or {}).get("ops") or [])
    except Exception:
        ops = set()
    _caps[uid] = (time.time(), ops)
    return ops


def forget(uid: Optional[int] = None) -> None:
    if uid is None:
        _caps.clear()
    else:
        _caps.pop(uid, None)


def available() -> bool:
    uid = _uid()
    if uid is None or not enabled():
        return False
    try:
        return _bridge().is_connected(uid) and OP in capabilities(uid)
    except Exception:
        return False


def recalculate(data: bytes, name: str = "workbook.xlsx") -> Optional[dict]:
    """{(sheet, 'A1'): value} for every formula cell as Excel calculated it (errors as their text), or None."""
    from ..doc_tools.constants import DEVICE_MAX_BYTES
    uid = _uid()
    if uid is None or not enabled() or len(data) > DEVICE_MAX_BYTES:
        return None
    try:
        if not (_bridge().is_connected(uid) and OP in capabilities(uid)):
            return None
        payload = {"data_b64": base64.b64encode(data).decode("ascii"), "name": name}
        res = _run(lambda: _bridge().call(uid, OP, payload, timeout=CALL_TIMEOUT_S))
    except Exception as e:
        print(f"[xlsx] Excel recalculation skipped ({type(e).__name__}: {str(e)[:120]})", file=sys.stderr)
        return None
    cells = (res or {}).get("cells")
    if not isinstance(cells, dict):
        return None
    from ..pan import mask_pans
    out = {}
    for key, v in cells.items():
        sheet, _, ref = str(key).rpartition("!")
        out[(sheet, ref)] = mask_pans(v)[0] if isinstance(v, str) else v
    try:
        from ..audit import audit_log
        from ..auth import _to_principal
        audit_log(_to_principal(uid), "sheet.excel_recalc", resource="workbook",
                  detail={"cells": len(out), "errors": int((res or {}).get("errors") or 0), "excel": str((res or {}).get("version") or "")})
    except Exception:
        pass
    return out


def status(uid: Optional[int]) -> dict:
    """What the Settings page shows: which engines will be used, in order."""
    from . import office
    try:
        connected = bool(uid is not None and _bridge().is_connected(uid))
    except Exception:
        connected = False
    has_op = bool(connected and OP in capabilities(uid)) if uid is not None else False
    lo = office.find_soffice()
    return {
        "order": ["Excel (your PC, through the companion)", "LibreOffice", "built-in calculator"],
        "excel": {"enabled": enabled(), "companion_connected": connected, "companion_supports_excel": has_op,
                  "note": "" if has_op else ("update the companion app to use Excel" if connected else "connect the companion app to use Excel")},
        "libreoffice": {"enabled": office.enabled(), "found": bool(lo), "path": lo or ""},
        "builtin": {"enabled": True},
    }
