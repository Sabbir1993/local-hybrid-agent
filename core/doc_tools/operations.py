import base64
from pathlib import Path
from .. import companion_bridge, doc_ops, doc_store
from ..agent_tools import _common_resolve, _ws_resolve, require_device_workspace
from ..doc_ops.base import sha256
from . import slide_images
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
    images, pic_note = {}, ""
    if Path(name).suffix.lower() == ".pptx" and not template and slide_images.wants_images(content):
        images, pic_notes, used_web = await slide_images.resolve(content, uid)
        pic_note = slide_images.summary(images, pic_notes, used_web)
    data, spec = doc_ops.create(name, content, template=template, images=images)
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
    if pic_note:
        msg += " " + pic_note
    if args.get("total") and target.suffix.lower() in _APPENDABLE:
        try:
            _BUILD_TOTAL[(uid if location == "common" else dev_uid, key)] = max(1, min(int(args["total"]), 30))
            msg += (f" It has {_unit_count(target.suffix.lower(), spec)} of {int(args['total'])}: add the rest in small "
                    f"steps with doc_append(file='{target.name}', content=<next 2-3 slides/sections>), then done=true.")
        except (TypeError, ValueError):
            pass
    return msg + (f" [DOWNLOAD: {target.name}]" if location == "common" else f" (saved on your device: {target})")


# Build-in-pieces bookkeeping: (uid, file key) -> slide/section count the user asked for. In memory on purpose:
# after a restart the count check is simply off, the document itself is unaffected.
_BUILD_TOTAL: dict = {}
_APPENDABLE = (".pptx", ".docx", ".pdf")


def _unit_count(ext: str, spec: str) -> int:
    """Slides in a deck, or top-level sections in a document, in the source markdown."""
    if ext == ".pptx":
        from ..doc_ops.pptx_ops import outline
        return len(outline.parse_outline(spec)["slides"])
    import re
    return len(re.findall(r"(?m)^#{1,2}\s+\S", spec))


def _leftover_markup(ext: str, spec: str) -> list:
    """Raw markdown that would show up as text in the finished file (checked on the final pass)."""
    import re
    if ext != ".pptx":
        return []
    from ..doc_ops.pptx_ops import outline
    bad = []
    for sl in outline.parse_outline(spec)["slides"]:
        for t in [sl["title"], sl.get("subtitle", "")] + [b.strip() for b in sl["bullets"]]:
            if re.search(r"\*\*|__|^#{1,6}\s|^>\s|^\[[ xX]\]", t or ""):
                bad.append(f"slide '{sl['title'][:40]}': {t[:50]}")
    return bad


async def _append(args: dict, location: str) -> str:
    """Add the next slides / sections to a document that is being built in pieces and rebuild it IN PLACE, so every
    step leaves a real, valid file (the right-hand preview refreshes from it). The markdown source is the truth:
    the final file is exactly what one write of the whole markdown would give."""
    name = _file_arg(args)
    content = str(args.get("content") or "").strip()
    done = bool(args.get("done"))
    if not content and not done:
        raise ValueError("content required (the next slides / sections as markdown)")
    loc, data = (await _locate(name)) if location == "device" else _locate_common(name)
    ext = loc.path.suffix.lower()
    if ext not in _APPENDABLE:
        raise ValueError(f"doc_append works on .pptx / .docx / .pdf, not {ext or 'this file'}")
    row, spec = _spec_for(loc)
    if not spec:
        raise ValueError("this file was not built here, so there is no source to extend - use doc_edit")
    key = (loc.uid, loc.key)
    if args.get("total"):
        try:
            _BUILD_TOTAL[key] = max(1, min(int(args["total"]), 30))
        except (TypeError, ValueError):
            pass
    new_spec = spec.rstrip()
    if content:
        new_spec += ("\n\n---\n\n" if ext == ".pptx" else "\n\n") + content
    have, total = _unit_count(ext, new_spec), _BUILD_TOTAL.get(key)
    if done and total and have < total:
        raise ValueError(f"not finished: {have} of {total} {'slides' if ext == '.pptx' else 'sections'} so far - "
                         "keep adding with doc_append (done=true only after the last one)")
    images, pic_note = {}, ""
    if ext == ".pptx" and slide_images.wants_images(new_spec):
        # pictures already found for earlier slides come from the resolver's cache, so a rebuild costs nothing extra
        images, pic_notes, used_web = await slide_images.resolve(new_spec, loc.uid)
        if slide_images.wants_images(content) or done:
            pic_note = slide_images.summary(images, pic_notes, used_web)
    if ext == ".pdf" and not done:
        out = data              # the PDF is printed once at the end; the preview shows a live HTML draft of the spec
    else:
        out, _ = doc_ops.create(loc.path.name, new_spec, images=images)
    if out is not data:
        await _write(loc, loc.path, out)
    doc_store.register(loc.uid, loc.key, ext.lstrip("."), location=loc.location,
                       parent_id=row["id"] if row else None, source_spec=new_spec, sha256=sha256(out))
    unit = "slides" if ext == ".pptx" else "sections"
    msg = f"Added to {loc.path.name}: now {have} {unit}" + (f" of {total}." if total else ".")
    if done:
        _BUILD_TOTAL.pop(key, None)
        bad = _leftover_markup(ext, new_spec)
        msg += " Finished" + (" - fix these with doc_edit set_text: " + "; ".join(bad[:5]) if bad else ".")
    elif total and have < total:
        msg += f" Continue with the next {min(3, total - have)} {unit} (doc_append), the last one is the closing."
    if pic_note:
        msg += " " + pic_note
    if loc.location == "common":
        msg += f" [DOWNLOAD: {loc.path.name}]"
    return msg
