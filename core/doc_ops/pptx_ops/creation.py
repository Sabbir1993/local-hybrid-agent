from typing import Optional
from .constants import (
    R_NS,
    _open,
    _save,
)
from .text_ops import _set_frame_text
from . import design, outline
from .outline import parse_markdown_slides  # noqa: F401  (kept importable from here)


def _flat(s: dict) -> list:
    """Bullets of a slide as plain lines (cards/stats/tables fold into the template's body placeholder)."""
    lines = [b for b in s["bullets"]]
    for row in s["table"]:
        lines.append(" | ".join(row))
    return lines


def _create_from_template(deck: dict, template: bytes) -> bytes:
    """Fill a user-supplied template: its theme and layouts are kept, only text is set."""
    prs = _open(template)
    lst = prs.slides._sldIdLst
    for el in list(lst):
        prs.part.drop_rel(el.get(f"{{{R_NS}}}id"))
        lst.remove(el)
    layouts = list(prs.slide_layouts)

    def pick(title_only: bool):
        for l in layouts:
            kinds = {str(p.placeholder_format.type).split(".")[-1].split(" ")[0] for p in l.placeholders}
            if title_only and {"CENTER_TITLE", "SUBTITLE"} & kinds:
                return l
            if not title_only and "TITLE" in kinds and ({"BODY", "OBJECT"} & kinds):
                return l
        return layouts[1 if len(layouts) > 1 else 0]

    for s in deck["slides"]:
        cover = s["kind"] in ("cover", "closing", "section")
        slide = prs.slides.add_slide(pick(cover))
        if slide.shapes.title is not None:
            slide.shapes.title.text_frame.text = s["title"]
        body = next((p for p in slide.placeholders if p.placeholder_format.idx != 0), None)
        lines = [s["subtitle"]] if cover and s["subtitle"] else _flat(s)
        lines = [l for l in lines if l]
        if body is not None:
            if lines:
                _set_frame_text(body.text_frame._txBody, "\n".join(l.rstrip(":") for l in lines))
            else:
                body._element.getparent().remove(body._element)
        if s["notes"]:
            slide.notes_slide.notes_text_frame.text = s["notes"]
    return _save(prs)


def create(md: str, template: Optional[bytes] = None, title: str = "") -> bytes:
    """Markdown / loose outline -> .pptx. Max outline.MAX_DECK_SLIDES slides; designed theme unless a template is given."""
    deck = outline.parse_outline(md, title)
    if template:
        return _create_from_template(deck, template)
    return _save(design.build(deck))
