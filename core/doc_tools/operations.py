import base64
from pathlib import Path
from .. import companion_bridge, doc_ops, doc_store
from ..agent_tools import _common_resolve, _ws_resolve, require_device_workspace
from ..doc_ops.base import sha256
from .location import (
    _Loc,
    _file_arg,
    _locate,
    _locate_common,
    _spec_for,
    _uid,
    _write,
    resolve_common,
)


def _inspect(loc: _Loc, data: bytes, args: dict) -> str:
    _row, spec = _spec_for(loc)
    text = doc_ops.inspect(data, loc.path.name, focus=args.get("focus"), source_spec=spec)
    where = "your device" if loc.location == "device" else "your files"
    return f"File: {loc.key} ({where})\n{text}"


async def _load_images(loc: _Loc, ops: list) -> None:
    """replace_image ops name an image file; load its bytes from the same place."""
    for op in ops:
        if str(op.get("op", "")).lower() == "replace_image" and op.get("image"):
            if loc.location == "device":
                d = await companion_bridge.call(loc.uid, "fs.read_b64", {"path": str(loc.sibling(Path(op["image"]).name))})
                op["_image_bytes"] = base64.b64decode(d["data"]) if d.get("data") else None
            else:
                p = resolve_common(op["image"])
                op["_image_bytes"] = p.read_bytes() if p else None


async def _edit(loc: _Loc, data: bytes, args: dict) -> str:
    ops = doc_ops.normalize_ops(args.get("ops"))
    await _load_images(loc, ops)
    row, spec = _spec_for(loc)
    out = doc_ops.edit(data, loc.path.name, ops, source_spec=spec)
    parent_id = row["id"] if row else doc_store.register(
        loc.uid, loc.key, loc.path.suffix.lower().lstrip("."), location=loc.location, sha256=sha256(data))
    version = (row["version"] if row else 1) + 1
    in_place = bool(args.get("in_place")) and loc.location == "device"
    target = loc.path if in_place else loc.sibling(doc_store.version_name(loc.path.name, version))
    await _write(loc, target, out.data)
    new_key = target.name if loc.location == "common" else str(target)
    doc_store.register(loc.uid, new_key, loc.path.suffix.lower().lstrip("."), location=loc.location,
                       parent_id=parent_id, source_spec=out.source_spec, sha256=sha256(out.data))
    doc_store.audit(loc.uid, "doc.edit", new_key, {"parent": loc.key, "ops": [o.get("op") for o in ops],
                                                   "sha256": sha256(out.data)})
    lines = [f"Edited {loc.path.name} -> saved as {target.name} (version {version}; the previous version is kept)."]
    lines += [f"- {c}" for c in out.changes]
    lines += [f"note: {n}" for n in out.notes]
    lines.append("Everything else in the document is unchanged (verified).")
    if loc.location == "common":
        lines.append(f"[DOWNLOAD: {target.name}]")
    return "\n".join(lines)


async def _create(args: dict, location: str) -> str:
    name = _file_arg(args)
    content = str(args.get("content") or args.get("spec") or "")
    template = None
    if args.get("template"):
        tloc, template = (await _locate(args["template"])) if location == "device" else _locate_common(args["template"])
    uid = _uid()
    data, spec = doc_ops.create(name, content, template=template)
    if location == "device":
        dev_uid, _ws = require_device_workspace()
        target = _ws_resolve(name)
        loc = _Loc("device", target, dev_uid)
        key = str(target)
    else:
        stem, suf = doc_store.base_stem(Path(name).name), Path(name).suffix.lower()
        target = _common_resolve(doc_store.version_name(f"{stem}{suf}", 1).replace("-v1-", "-"))
        loc = _Loc("common", target, uid)
        key = target.name
    await _write(loc, target, data)
    doc_store.register(uid, key, target.suffix.lower().lstrip("."), location=location, source_spec=spec,
                       sha256=sha256(data))
    msg = f"Created {target.name}. Later changes can be made with doc_edit without regenerating it."
    return msg + (f" [DOWNLOAD: {target.name}]" if location == "common" else f" (saved on your device: {target})")
