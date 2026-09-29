from .constants import (
    MAX_CELLS_SHOWN,
    _body_children,
    _cells,
    _clip,
    _open,
    _paras,
    _ptext,
    _rows,
    _style,
    _tables,
    _w,
)


def inspect(data: bytes) -> dict:
    doc = _open(data)
    els = []
    paras, tables = _paras(doc), _tables(doc)
    head = f"Word document: {len(paras)} paragraphs, {len(tables)} tables, {len(doc.sections)} sections"
    pi = ti = 0
    for el in _body_children(doc):
        if el.tag == _w("p"):
            pi += 1
            t = _ptext(el)
            if t.strip():
                st = _style(doc, el)
                els.append({"addr": f"p{pi}", "kind": st or "para", "text": _clip(t)})
        elif el.tag == _w("tbl"):
            ti += 1
            rows = _rows(el)
            els.append({"addr": f"t{ti}", "kind": "table", "text": f"{len(rows)} rows"})
            n = 0
            for r, tr in enumerate(rows, 1):
                for c, tc in enumerate(_cells(tr), 1):
                    if n < MAX_CELLS_SHOWN:
                        els.append({
                            "addr": f"t{ti}[{r},{c}]",
                            "kind": "cell",
                            "text": _clip(" / ".join(_ptext(p) for p in tc.iter(_w("p"))), 120),
                        })
                    n += 1
    for si, sec in enumerate(doc.sections, 1):
        for kind, hf in (("hdr", sec.header), ("ftr", sec.footer)):
            if hf.is_linked_to_previous:
                continue
            for j, p in enumerate(hf._element.findall(_w("p")), 1):
                if _ptext(p).strip():
                    els.append({
                        "addr": f"{kind}{si}/p{j}",
                        "kind": "header" if kind == "hdr" else "footer",
                        "text": _clip(_ptext(p)),
                    })
    return {"kind": "docx", "header": head, "elements": els}
