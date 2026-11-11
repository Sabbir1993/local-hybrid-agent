import base64
import re
from pathlib import Path
from typing import Optional
from .. import companion_bridge, doc_store
from ..agent_tools.workspace import (
    _common_resolve,
    _ws_resolve,
    common_workspace,
    require_device_workspace,
)
from ..doc_ops.base import DocOpError
from ..request_context import get_current_user_id
from .constants import DEVICE_MAX_BYTES


def _uid() -> int:
    uid = get_current_user_id()
    if uid is None:
        raise PermissionError("not signed in")
    return uid


def _file_arg(args: dict) -> str:
    f = args.get("file") or args.get("path") or args.get("filename") or args.get("name")
    if not f:
        # Fallback 1: check if there is an active open build in deck_builds
        try:
            from . import builds
            uid = _uid()
            b = builds.latest_open(uid)
            if b and b.get("file"):
                return str(b["file"]).strip()
        except Exception:
            pass
        # Fallback 2: check if there is a recent appendable file in the caller's workspace
        try:
            folder = common_workspace()
            recent = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in (".pptx", ".pdf", ".docx")]
            if recent:
                recent.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                return recent[0].name
        except Exception:
            pass
        raise ValueError("file required")
    return str(f).strip()



def resolve_common(name: str) -> Optional[Path]:
    """The caller's common-space file: exact name, else the newest version of it."""
    try:
        p = _common_resolve(name)
    except PermissionError:
        return None
    if p.is_file():
        return p
    folder = common_workspace()

    # Normalize stuttered or duplicated extensions / stems (e.g. .html.html -> .html, annual_annual -> annual)
    clean_name = re.sub(r'(\.[a-zA-Z0-9]+)\1+', r'\1', str(name or ""))
    clean_name = re.sub(r'([a-zA-Z0-9]+)(?:_\1)+', r'\1', clean_name)
    if clean_name != name:
        try:
            p_clean = _common_resolve(clean_name)
            if p_clean.is_file():
                return p_clean
        except PermissionError:
            pass

    stem, suf = doc_store.base_stem(Path(name).name), Path(clean_name).suffix.lower()
    cands = [f for f in folder.glob(f"{stem}*{suf}") if f.is_file() and doc_store.base_stem(f.name) == stem]
    if cands:
        return max(cands, key=lambda f: f.stat().st_mtime)

    # Fuzzy recovery 1: match on 8-char hex hash suffix if present in name
    # Only the build id our own files end with ("report-1a2b3c4d.xlsx"), and only when one file has it: any 8 digits
    # would also match a date (20250101) or an employee ID, i.e. somebody else's file.
    m_hex = re.search(r'[-_]([0-9a-fA-F]{8})$', Path(clean_name).stem)
    if m_hex and re.search(r"[a-fA-F]", m_hex.group(1)):      # eight plain digits are a date or an ID, not a build id
        hex_sub = m_hex.group(1).lower()
        hex_cands = [f for f in folder.glob(f"*{suf}") if f.is_file() and f.stem.lower().endswith(hex_sub)]
        if len(hex_cands) == 1:
            return hex_cands[0]

    # Fuzzy recovery 2: normalize spaces, unicode numbers, typos and match skeleton
    import unicodedata
    norm_name = unicodedata.normalize('NFKD', str(name or ''))
    norm_name = re.sub(r'[₀-₉]', lambda m: str(ord(m.group(0)) - ord('₀')), norm_name)
    norm_name = re.sub(r'([0-9])\s+([0-9])', r'\1\2', norm_name)
    norm_name = re.sub(r'([a-zA-Z])\s+([a-zA-Z])', r'\1\2', norm_name)
    norm_name = re.sub(r'2[oO]26', '2026', norm_name)
    sk_query = re.sub(r'[^a-zA-Z0-9]', '', doc_store.base_stem(norm_name)).lower()

    suf_files = [f for f in folder.glob(f"*{suf}") if f.is_file()]
    if not suf_files and suf:
        suf_files = [f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in ('.xlsx', '.xls', '.csv', '.docx', '.pptx', '.pdf')]
    if suf_files:
        for f in suf_files:
            f_sk = re.sub(r'[^a-zA-Z0-9]', '', doc_store.base_stem(f.name)).lower()
            if f_sk == sk_query:
                return f
            # (no "collapse repeated characters" match: it equates IDs like 1550 and 150, i.e. another person's record)
        clean_stem = doc_store.base_stem(Path(clean_name).name).lower()
        matching = [f for f in suf_files if (doc_store.base_stem(f.name).lower() in clean_stem or clean_stem in doc_store.base_stem(f.name).lower())]
        # Only an unambiguous match: files that differ by one name or ID (one employee's record vs another's) must never
        # be swapped silently. Several matches -> None, and the caller lists the real names (data_file_choices).
        if len(matching) == 1:
            return matching[0]

    return None


class _Loc:
    """Where a document lives and how to write its next version."""

    def __init__(self, location: str, path: Path, uid: int):
        self.location, self.path, self.uid = location, path, uid

    @property
    def key(self) -> str:
        return self.path.name if self.location == "common" else str(self.path)

    def sibling(self, name: str) -> Path:
        if self.location == "common":
            return _common_resolve(name)
        return self.path.with_name(name)


async def _locate(name: str) -> tuple[_Loc, bytes]:
    uid = _uid()
    # 1. the project folder on the user's device (agent mode)
    try:
        dev_uid, _ws = require_device_workspace()
        p = _ws_resolve(name)
        data = await companion_bridge.call(dev_uid, "fs.read_b64", {"path": str(p)})
        if data.get("data") is not None:
            return _Loc("device", p, dev_uid), base64.b64decode(data["data"])
    except PermissionError:   # includes WorkspaceAccessDenied: no project on this device
        pass
    except RuntimeError as e:
        if "unknown op" in str(e):
            raise DocOpError("the SSL Local Agent app on your machine is too old for document edits - "
                             "update it and try again")
        raise DocOpError(f"could not read the file on your device: {e}")
    # 2. the user's own common space (chat files, uploaded attachments)
    p = resolve_common(name)
    if p is None:
        raise FileNotFoundError(f"document not found: {name}")
    return _Loc("common", p, uid), p.read_bytes()


def _locate_common(name: str) -> tuple[_Loc, bytes]:
    p = resolve_common(name)
    if p is None:
        raise FileNotFoundError(f"document not found: {name}")
    return _Loc("common", p, _uid()), p.read_bytes()


async def _write(loc: _Loc, target: Path, data: bytes) -> None:
    if loc.location == "common":
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return
    if len(data) > DEVICE_MAX_BYTES:
        raise DocOpError(f"document too large for the companion ({len(data)} bytes)")
    await companion_bridge.call(loc.uid, "fs.write_b64", {"path": str(target),
                                                          "data": base64.b64encode(data).decode("ascii")})


def _spec_for(loc: _Loc) -> tuple[Optional[dict], Optional[str]]:
    row = doc_store.find(loc.uid, loc.key, loc.location)
    return row, (row or {}).get("source_spec")


def _err(e: Exception) -> str:
    if isinstance(e, (DocOpError, FileNotFoundError, PermissionError, ValueError)):
        return f"doc error: {e}"
    raise e


DATA_SUFFIXES = (".xlsx", ".xls", ".csv")


def data_file_choices(name: str, limit: int = 12) -> str:
    """Error text for a data file name that matched nothing: the closest real names and the user's data files.

    Suggests only. Attachments such as per-employee records differ by one name or ID, so picking the "closest" file
    automatically could answer from the wrong person's data; the model has to copy a real name and call again."""
    import difflib
    try:
        folder = common_workspace()
        files = sorted((f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in DATA_SUFFIXES),
                       key=lambda f: f.stat().st_mtime, reverse=True)
    except (OSError, PermissionError):
        files = []
    names = [f.name for f in files]
    msg = f"error: no attached data file named '{name}'."
    if not names:
        return msg + " No data files are attached in this workspace."
    close = difflib.get_close_matches(Path(str(name or "")).name, names, n=3, cutoff=0.6)
    if close:
        msg += " Did you mean: " + ", ".join(f"'{c}'" for c in close) + "?"
    shown = names[:limit]
    msg += " Attached data files: " + ", ".join(f"'{n}'" for n in shown)
    if len(names) > limit:
        msg += f" (+{len(names) - limit} more)"
    return msg + ". Copy one name exactly and call again."
