import copy
from lxml import etree
from ..base import require
from .constants import _a


def _set_para_text(p_el, text: str) -> None:
    """Replace a paragraph's text, keeping pPr (bullets/alignment) and the
    first run's rPr (font/size/colour)."""
    text = text.replace("\v", " ")
    runs = p_el.findall(_a("r"))
    for el in list(p_el):
        if el.tag in (_a("br"), _a("fld")):
            p_el.remove(el)
    if runs:
        first = runs[0]
        for r in runs[1:]:
            p_el.remove(r)
    else:
        first = etree.SubElement(p_el, _a("r"))
        end = p_el.find(_a("endParaRPr"))
        if end is not None:
            rpr = copy.deepcopy(end)
            rpr.tag = _a("rPr")
            first.insert(0, rpr)
            p_el.remove(first)
            end.addprevious(first)
    t = first.find(_a("t"))
    if t is None:
        t = etree.SubElement(first, _a("t"))
    t.text = text


def _set_frame_text(txBody, text: str) -> None:
    """Replace the text of a text body; one paragraph per line, extra lines
    clone the last paragraph's formatting. Leading tabs set the bullet level."""
    lines = str(text).split("\n")
    paras = txBody.findall(_a("p"))
    if not paras:
        paras = [etree.SubElement(txBody, _a("p"))]
    for i, line in enumerate(lines):
        stripped = line.lstrip("\t ")
        lead = line[:len(line) - len(stripped)]
        level = lead.count("\t") + lead.count(" ") // 2
        if i < len(paras):
            p = paras[i]
        else:
            p = copy.deepcopy(paras[-1])
            txBody.findall(_a("p"))[-1].addnext(p)
        _set_para_text(p, stripped)
        ppr = p.find(_a("pPr"))
        if level:
            if ppr is None:
                ppr = etree.Element(_a("pPr"))
                p.insert(0, ppr)
            ppr.set("lvl", str(min(level, 8)))
        elif ppr is not None and "lvl" in ppr.attrib:
            del ppr.attrib["lvl"]
    for p in paras[len(lines):]:
        txBody.remove(p)


def _txbody_of(sh):
    require(sh.has_text_frame, f"shape sh{sh.shape_id} has no text")
    return sh.text_frame._txBody


def _para_text(p_el) -> str:
    return "".join((t.text or "") for t in p_el.iter(_a("t")))


def replace_in_paragraph(p_el, find: str, repl: str) -> bool:
    """Replace inside single runs when possible (keeps per-run formatting);
    fall back to rewriting the paragraph when a match spans runs."""
    full = _para_text(p_el)
    if find not in full:
        return False
    ts = list(p_el.iter(_a("t")))
    if sum((t.text or "").count(find) for t in ts) == full.count(find):
        for t in ts:
            if t.text and find in t.text:
                t.text = t.text.replace(find, repl)
    else:
        _set_para_text(p_el, full.replace(find, repl))
    return True
