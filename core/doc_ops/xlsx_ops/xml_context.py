import posixpath
from typing import Optional
from lxml import etree
from ..base import DocOpError, PartRules, Snapshot, xml_fp
from .book import _Book
from .constants import (
    RT_TABLE,
    _range_bounds,
    _s,
    num_to_col,
)


def _sheet_items(name: str, root):
    items = []
    for el in root:
        ln = etree.QName(el).localname
        if ln == "sheetData":
            for row in el.iterfind(_s("row")):
                for cel in row.iterfind(_s("c")):
                    items.append((f"{name}!{cel.get('r')}", cel))
        elif ln != "dimension":
            items.append((f"{name}!<{ln}>", el))
    return items


class _Ctx:
    def __init__(self, book: _Book):
        self.book = book
        self.snap = Snapshot(xml_fp)
        self.rules = PartRules()
        self.changes: list[str] = []
        self.notes: list[str] = []
        self.formula_changed = False
        self.cells_changed = False
        self._snapped: set[str] = set()
        self._merged: dict[str, list] = {}
        self._tables: dict[str, list] = {}

    def sheet(self, name: Optional[str]):
        n, part = self.book.sheet_part(name)
        root = self.book.tree(part)
        if part not in self._snapped:
            self._snapped.add(part)
            self.snap.take(_sheet_items(n, root))
            self._merged[part] = [_range_bounds(m.get("ref")) for m in root.iter(_s("mergeCell"))]
            self._tables[part] = self._load_tables(part)
        return n, part, root

    def _load_tables(self, part: str) -> list:
        out = []
        for r in self.book.rels_of(part):
            if r.get("Type") == RT_TABLE:
                tp = posixpath.normpath(posixpath.join(posixpath.dirname(part), r.get("Target")))
                if tp in self.book.parts:
                    t = self.book.tree(tp)
                    out.append((tp, t))
        return out

    def check_writable(self, part: str, r: int, c: int, cel):
        for (r1, c1, r2, c2) in self._merged[part]:
            if r1 <= r <= r2 and c1 <= c <= c2 and (r, c) != (r1, c1):
                raise DocOpError(f"{num_to_col(c)}{r} is inside merged range; edit the top-left cell "
                                 f"{num_to_col(c1)}{r1}")
        for _tp, t in self._tables[part]:
            r1, c1, r2, c2 = _range_bounds(t.get("ref"))
            if r == r1 and c1 <= c <= c2 and t.get("headerRowCount", "1") != "0":
                raise DocOpError(f"{num_to_col(c)}{r} is a table header (table '{t.get('displayName')}'); "
                                 "renaming table columns is not supported")
        if cel is not None:
            f = cel.find(_s("f"))
            if f is not None and f.get("ref") and f.get("t") in ("shared", "array"):
                r1, c1, r2, c2 = _range_bounds(f.get("ref"))
                if (r1, c1) != (r2, c2):
                    raise DocOpError(f"{num_to_col(c)}{r} holds a {f.get('t')} formula for {f.get('ref')}; "
                                     "editing it would break the other cells")
