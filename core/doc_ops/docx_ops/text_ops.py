import copy
from lxml import etree
from ..base import DocOpError
from .constants import _KEEP, _ptext, _w


def set_para_text(p, text: str) -> None:
    """Replace the paragraph text: pPr (style, numbering) and the first run's
    rPr (font, bold, colour) are kept; other runs are removed."""
    runs = list(p.iter(_w("r")))
    first = runs[0] if runs else None
    if first is not None and first.getparent() is not p:
        # lift the run out of a hyperlink / tracked-change wrapper
        first.getparent().addprevious(first)
    for el in list(p):
        if el is first or etree.QName(el).localname in _KEEP:
            continue
        p.remove(el)
    if first is None:
        first = etree.SubElement(p, _w("r"))
    for ch in list(first):
        if ch.tag != _w("rPr"):
            first.remove(ch)
    lines = str(text).split("\n")
    for i, line in enumerate(lines):
        if i:
            etree.SubElement(first, _w("br"))
        parts = line.split("\t")
        for j, seg in enumerate(parts):
            if j:
                etree.SubElement(first, _w("tab"))
            t = etree.SubElement(first, _w("t"))
            t.text = seg
            t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")


def replace_in_para(p, find: str, repl: str) -> bool:
    full = _ptext(p)
    if find not in full:
        return False
    ts = list(p.iter(_w("t")))
    if sum((t.text or "").count(find) for t in ts) == full.count(find):
        for t in ts:
            if t.text and find in t.text:
                t.text = t.text.replace(find, repl)
                t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    else:
        set_para_text(p, full.replace(find, repl))
    return True


def _set_cell_text(cell, text: str):
    ps = cell.findall(_w("p"))
    lines = str(text).split("\n")
    for i, line in enumerate(lines):
        if i < len(ps):
            p = ps[i]
        else:
            p = copy.deepcopy(ps[-1])
            cell.findall(_w("p"))[-1].addnext(p)
        set_para_text(p, line)
    for p in ps[len(lines):]:
        cell.remove(p)


def _style_id(doc, name: str) -> str:
    for s in doc.styles:
        if s.name and s.name.lower() == str(name).lower():
            return s.style_id
    raise DocOpError(f"style '{name}' not found in this document")
