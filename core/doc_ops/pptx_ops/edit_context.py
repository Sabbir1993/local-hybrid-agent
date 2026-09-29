import copy
from lxml import etree
from ..base import DocOpError, PartRules, Snapshot, parse_xml, xml_fp
from .constants import (
    P_NS,
    R_NS,
    RT_LAYOUT,
    RT_NOTES,
)


def _snapshot_items(prs):
    items = [("slide order", prs.slides._sldIdLst)]
    for i, slide in enumerate(prs.slides, 1):
        spTree = slide.shapes._spTree
        for el in spTree:
            if etree.QName(el).localname in ("nvGrpSpPr", "grpSpPr"):
                continue
            nv = el[0].find(f"{{{P_NS}}}cNvPr") if len(el) else None
            items.append((f"s{i}/sh{nv.get('id') if nv is not None else '?'}", el))
        root = slide._element
        for el in root:
            if etree.QName(el).localname != "cSld":
                items.append((f"s{i}/{etree.QName(el).localname}", el))
        bg = root.find(f"{{{P_NS}}}cSld/{{{P_NS}}}bg")
        if bg is not None:
            items.append((f"s{i}/background", bg))
        if slide.has_notes_slide:
            items.append((f"s{i}/notes", slide.notes_slide._element))
    return items


def _rename_parts(parts: dict[str, bytes]) -> dict[str, str]:
    """python-pptx renumbers slide parts on save; key them by the slide id instead."""
    out = {}
    try:
        pres = parse_xml(parts["ppt/presentation.xml"])
        rels = parse_xml(parts["ppt/_rels/presentation.xml.rels"])
    except KeyError:
        return out
    r_target = {r.get("Id"): r.get("Target") for r in rels}
    for sld in pres.iter(f"{{{P_NS}}}sldId"):
        tgt = r_target.get(sld.get(f"{{{R_NS}}}id"))
        if not tgt:
            continue
        name = tgt.lstrip("/") if tgt.startswith("/") else "ppt/" + tgt
        name = name.replace("ppt/../", "")
        key = f"slide:{sld.get('id')}"
        out[name] = key
        d, f = name.rsplit("/", 1)
        srels = parts.get(f"{d}/_rels/{f}.rels")
        if srels:
            for r in parse_xml(srels):
                if r.get("Type") == RT_NOTES:
                    import posixpath
                    out[posixpath.normpath(posixpath.join(d, r.get("Target")))] = f"notes:{sld.get('id')}"
    return out


class _Ctx:
    def __init__(self, prs):
        self.prs = prs
        self.snap = Snapshot(xml_fp)
        self.snap.take(_snapshot_items(prs))
        self.rules = PartRules()
        self.changes: list[str] = []
        self.notes: list[str] = []
        self.images: dict[str, bytes] = {}

    def sid(self, slide) -> int:
        return slide.slide_id

    def text_edit(self, slide, el):
        self.snap.touch(el)
        self.rules.changed.add(f"slide:{slide.slide_id}")

    def structural(self):
        self.snap.touch(self.prs.slides._sldIdLst)
        self.rules.changed |= {"ppt/presentation.xml", "ppt/presentation.xml:rels", "[Content_Types].xml"}

    def new_slide(self, slide):
        k = f"slide:{slide.slide_id}"
        self.rules.added_prefixes |= {k, k + ":rels"}
        self.snap.touch(slide._element, ancestors=False)
        for el in slide._element.iter():
            self.snap.touch(el, ancestors=False)


def _move_sldId(prs, sldId_el, position: int):
    lst = prs.slides._sldIdLst
    lst.remove(sldId_el)
    lst.insert(position, sldId_el)


def _sldId_of(prs, slide):
    for el in prs.slides._sldIdLst:
        if int(el.get("id")) == slide.slide_id:
            return el
    raise DocOpError("slide id not found")


def _duplicate(ctx: _Ctx, src):
    prs = ctx.prs
    new = prs.slides.add_slide(src.slide_layout)
    tree = new.shapes._spTree
    for el in list(tree):
        if etree.QName(el).localname not in ("nvGrpSpPr", "grpSpPr"):
            tree.remove(el)
    rid_map = {}
    for rid, rel in src.part.rels.items():
        if rel.reltype in (RT_LAYOUT, RT_NOTES):
            continue
        if rel.is_external:
            rid_map[rid] = new.part.relate_to(rel.target_ref, rel.reltype, is_external=True)
        else:
            rid_map[rid] = new.part.relate_to(rel.target_part, rel.reltype)
    for el in src.shapes._spTree:
        if etree.QName(el).localname in ("nvGrpSpPr", "grpSpPr"):
            continue
        tree.append(copy.deepcopy(el))
    src_bg = src._element.find(f"{{{P_NS}}}cSld/{{{P_NS}}}bg")
    if src_bg is not None:
        new._element.find(f"{{{P_NS}}}cSld").insert(0, copy.deepcopy(src_bg))
    for el in new._element.iter():
        for attr in list(el.attrib):
            if attr.startswith(f"{{{R_NS}}}") and el.get(attr) in rid_map:
                el.set(attr, rid_map[el.get(attr)])
    ctx.new_slide(new)
    return new
