import base64
from pathlib import Path
from typing import Optional
from .. import companion_bridge, doc_store
from ..agent_tools import (
    _common_resolve,
    _ws_resolve,
    common_workspace,
    get_current_user_id,
    require_device_workspace,
)
from ..doc_ops.base import DocOpError
from .constants import DEVICE_MAX_BYTES


def _uid() -> int:
    uid = get_current_user_id()
    if uid is None:
        raise PermissionError("not signed in")
    return uid


def _file_arg(args: dict) -> str:
    f = args.get("file") or args.get("path") or args.get("filename") or args.get("name")
    if not f:
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
    stem, suf = doc_store.base_stem(Path(name).name), Path(name).suffix.lower()
    cands = [f for f in folder.glob(f"{stem}*{suf}") if f.is_file() and doc_store.base_stem(f.name) == stem]
    return max(cands, key=lambda f: f.stat().st_mtime) if cands else None


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
            raise DocOpError("the A770 Companion app on your machine is too old for document edits - "
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
