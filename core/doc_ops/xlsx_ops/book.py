import io
import posixpath
import zipfile
from typing import Any, Optional
from lxml import etree
from ..base import DocOpError, parse_xml, read_zip, require
from .constants import R_NS, _s


class _Book:
    def __init__(self, data: bytes):
        self.data = data
        self.parts = read_zip(data)
        require("xl/workbook.xml" in self.parts, "not an Excel workbook")
        self.wb = parse_xml(self.parts["xl/workbook.xml"])
        rels = parse_xml(self.parts.get("xl/_rels/workbook.xml.rels", b"<Relationships/>"))
        self.wb_rels = rels
        rid_target = {r.get("Id"): r.get("Target") for r in rels}
        self.sheets: list[tuple[str, str]] = []   # (name, part name)
        for sh in self.wb.iter(_s("sheet")):
            tgt = rid_target.get(sh.get(f"{{{R_NS}}}id"), "")
            part = tgt.lstrip("/") if tgt.startswith("/") else posixpath.normpath(posixpath.join("xl", tgt))
            if part in self.parts:
                self.sheets.append((sh.get("name"), part))
        require(bool(self.sheets), "workbook has no worksheets")
        self.trees: dict[str, Any] = {}
        self.dirty: set[str] = set()
        self.removed: set[str] = set()

    def sheet_part(self, name: Optional[str]) -> tuple[str, str]:
        if name is None:
            return self.sheets[0]
        for n, p in self.sheets:
            if n == name:
                return n, p
        for n, p in self.sheets:
            if n.lower() == name.lower():
                return n, p
        raise DocOpError(f"sheet '{name}' not found (have: {', '.join(n for n, _ in self.sheets)})")

    def tree(self, part: str):
        if part not in self.trees:
            self.trees[part] = parse_xml(self.parts[part])
        return self.trees[part]

    def rels_of(self, part: str) -> list:
        d, f = posixpath.split(part)
        blob = self.parts.get(f"{d}/_rels/{f}.rels")
        return list(parse_xml(blob)) if blob else []

    def has_formulas(self) -> bool:
        return any(b"<f" in self.parts[p] or b":f" in self.parts[p] for _n, p in self.sheets)

    def save(self) -> bytes:
        for part in self.dirty:
            self.parts[part] = etree.tostring(
                self.trees[part],
                xml_declaration=True,
                encoding="UTF-8",
                standalone=True,
            )
        src = zipfile.ZipFile(io.BytesIO(self.data))
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for info in src.infolist():
                name = info.filename.lstrip("/")
                if name in self.removed:
                    continue
                if name in self.dirty:
                    z.writestr(info, self.parts[name], compress_type=zipfile.ZIP_DEFLATED)
                else:
                    z.writestr(info, src.read(info))   # untouched part: byte-identical
        return out.getvalue()
