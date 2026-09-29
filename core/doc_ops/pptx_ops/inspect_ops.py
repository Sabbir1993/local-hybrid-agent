from .constants import (
    MAX_TABLE_CELLS_SHOWN,
    _clip,
    _iter_shapes,
    _open,
    _shape_kind,
)


def inspect(data: bytes) -> dict:
    prs = _open(data)
    els = []
    w, h = prs.slide_width or 0, prs.slide_height or 0
    ratio = "16:9" if h and abs(w / h - 16 / 9) < 0.05 else ("4:3" if h and abs(w / h - 4 / 3) < 0.05 else "custom")
    layouts = sorted({l.name for m in prs.slide_masters for l in m.slide_layouts})
    head = f"PowerPoint: {len(prs.slides)} slides ({ratio}). Layouts: {', '.join(layouts)}"
    for i, slide in enumerate(prs.slides, 1):
        els.append({"addr": f"s{i}", "kind": "slide", "text": f"layout: {slide.slide_layout.name}"})
        for sh in _iter_shapes(slide.shapes):
            base = f"s{i}/sh{sh.shape_id}"
            kind = _shape_kind(sh)
            if kind == "table":
                n = 0
                for r, row in enumerate(sh.table.rows, 1):
                    for c, cell in enumerate(row.cells, 1):
                        if n < MAX_TABLE_CELLS_SHOWN:
                            els.append({"addr": f"{base}/tbl[{r},{c}]", "kind": "cell", "text": _clip(cell.text, 120)})
                        n += 1
                els.append({"addr": base, "kind": "table",
                            "text": f"{len(sh.table.rows)}x{len(sh.table.columns)} '{sh.name}'"})
                continue
            if kind == "chart":
                ch = sh.chart
                title = ch.chart_title.text_frame.text if ch.has_title else ""
                series = [s.name for p in ch.plots for s in p.series]
                els.append({"addr": base, "kind": "chart", "text": f"title='{title}' series={series} (read-only)"})
                continue
            if kind == "picture":
                alt = sh._element.xpath("./p:nvPicPr/p:cNvPr/@descr")
                els.append({"addr": base, "kind": "picture", "text": f"'{sh.name}' alt='{alt[0] if alt else ''}'"})
                continue
            if kind == "group":
                els.append({"addr": base, "kind": "group", "text": f"'{sh.name}'"})
                continue
            if sh.has_text_frame:
                paras = sh.text_frame.paragraphs
                if len(paras) <= 1:
                    els.append({"addr": base, "kind": kind, "text": _clip(sh.text_frame.text)})
                else:
                    els.append({"addr": base, "kind": kind, "text": f"{len(paras)} paragraphs"})
                    for j, p in enumerate(paras, 1):
                        lvl = "  " * p.level
                        els.append({"addr": f"{base}/p{j}", "kind": "para", "text": lvl + _clip(p.text, 200)})
        if slide.has_notes_slide:
            nt = slide.notes_slide.notes_text_frame
            if nt is not None and nt.text.strip():
                els.append({"addr": f"s{i}/notes", "kind": "notes", "text": _clip(nt.text)})
    return {"kind": "pptx", "header": head, "elements": els}
