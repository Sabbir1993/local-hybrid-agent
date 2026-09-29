import io
import re
from lxml import etree
from ..base import read_zip

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
MAX_CELLS_SHOWN = 400
_KEEP = ("pPr", "bookmarkStart", "bookmarkEnd", "commentRangeStart", "commentRangeEnd", "proofErr")
_ADDR = re.compile(r"^(?:(hdr|ftr)(\d+)/)?(?:p(\d+)|t(\d+)(?:[\[(](\d+)\s*,\s*(\d+)[\])])?)$", re.I)


def _w(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


def _open(data: bytes):
    import docx
    read_zip(data)
    return docx.Document(io.BytesIO(data))


def _body_children(doc):
    return [el for el in doc.element.body if etree.QName(el).localname in ("p", "tbl", "sdt")]


def _paras(doc):
    return [el for el in doc.element.body if el.tag == _w("p")]


def _tables(doc):
    return [el for el in doc.element.body if el.tag == _w("tbl")]


def _ptext(p) -> str:
    out = []
    for el in p.iter(_w("t"), _w("tab"), _w("br")):
        if el.tag == _w("t"):
            out.append(el.text or "")
        else:
            out.append("\t" if el.tag == _w("tab") else "\n")
    return "".join(out)


def _style(doc, p) -> str:
    ps = p.find(f"{_w('pPr')}/{_w('pStyle')}")
    if ps is None:
        return ""
    sid = ps.get(_w("val"))
    try:
        return doc.styles.element.get_by_id(sid).name_val or sid
    except Exception:
        return sid or ""


def _clip(s: str, n: int = 300) -> str:
    s = s.strip()
    return s if len(s) <= n else s[:n] + "..."


def _rows(tbl):
    return tbl.findall(_w("tr"))


def _cells(tr):
    return tr.findall(_w("tc"))
