"""Shared machinery for surgical document edits.

Two independent guarantees back "only the asked part changes":

1. Element snapshot (in memory): before the ops run, every addressable element
   (slide shape, sheet cell, body paragraph, CSV row ...) is fingerprinted by
   object identity. Ops `touch()` what they modify; afterwards any untouched
   element whose fingerprint moved, or that vanished/appeared, is an error.
2. Package parts (OOXML bytes): every part of the zip is canonicalised
   (C14N for XML, a sorted relation set for .rels / [Content_Types].xml) before
   and after. A part the ops were not allowed to touch must come out identical.
"""

import hashlib
import io
import posixpath
import zipfile
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from lxml import etree

MAX_DOC_BYTES = 50 * 1024 * 1024          # accepted input size (server side)
MAX_UNZIPPED_BYTES = 400 * 1024 * 1024    # zip-bomb guard
MAX_ZIP_PARTS = 5000
MAX_COMPRESSION_RATIO = 200

# No DTD/entity resolution, no network: uploaded OOXML is untrusted input.
XML_PARSER = etree.XMLParser(remove_blank_text=True, resolve_entities=False,
                             no_network=True, huge_tree=False)

MACRO_EXTS = {".pptm", ".xlsm", ".docm", ".potm", ".xltm", ".dotm", ".ppsm"}


class DocOpError(ValueError):
    """An edit that cannot be applied safely. The message is shown to the model."""


def digest(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()[:20]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def check_size(data: bytes, limit: int = MAX_DOC_BYTES) -> None:
    if len(data) > limit:
        raise DocOpError(f"document is too large ({len(data)} bytes, max {limit})")


def read_zip(data: bytes) -> dict[str, bytes]:
    """All parts of an OOXML package, with zip-bomb / macro guards."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise DocOpError("not a valid Office Open XML file (zip)")
    infos = zf.infolist()
    if len(infos) > MAX_ZIP_PARTS:
        raise DocOpError("document has too many parts")
    total = 0
    for i in infos:
        total += i.file_size
        if i.compress_size and i.file_size / max(i.compress_size, 1) > MAX_COMPRESSION_RATIO and i.file_size > 10 * 1024 * 1024:
            raise DocOpError("document looks like a zip bomb")
    if total > MAX_UNZIPPED_BYTES:
        raise DocOpError("document is too large when unpacked")
    parts = {i.filename.lstrip("/"): zf.read(i) for i in infos if not i.is_dir()}
    if any(n.lower().endswith("vbaproject.bin") for n in parts):
        raise DocOpError("macro-enabled documents are not supported")
    return parts


def parse_xml(data: bytes):
    return etree.fromstring(data, XML_PARSER)


def c14n(elem) -> bytes:
    return etree.tostring(elem, method="c14n")


# ------------------------------------------------------------------
# Package-level verification
# ------------------------------------------------------------------

def _rels_source(rels_name: str) -> str:
    # ppt/slides/_rels/slide1.xml.rels -> ppt/slides/slide1.xml ; _rels/.rels -> ""
    d, f = posixpath.split(rels_name)
    base = posixpath.dirname(d)
    return posixpath.join(base, f[:-5]) if f != ".rels" else ""


def _resolve_target(source: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(source), target))


def canon_parts(parts: dict[str, bytes], rename: Optional[dict[str, str]] = None
                ) -> tuple[dict[str, str], set[str]]:
    """({stable key: digest}, reachable keys). `rename` maps part names that a
    library may renumber on save (e.g. python-pptx slide parts) to stable keys."""
    rename = rename or {}
    key = lambda n: rename.get(n, n)  # noqa: E731
    out: dict[str, str] = {}
    reachable: set[str] = set()
    for name, blob in parts.items():
        low = name.lower()
        try:
            if low.endswith(".rels"):
                src = _rels_source(name)
                rels = []
                for r in parse_xml(blob):
                    tgt = r.get("Target", "")
                    ext = r.get("TargetMode") == "External"
                    if not ext:
                        tgt = key(_resolve_target(src, tgt))
                        reachable.add(tgt)
                    rels.append((r.get("Id", ""), r.get("Type", ""), tgt, "ext" if ext else ""))
                k = (key(src) + ":rels") if src else "_rels/.rels"
                out[k] = digest(repr(sorted(rels)).encode())
            elif name == "[Content_Types].xml":
                items = []
                for e in parse_xml(blob):
                    a = dict(e.attrib)
                    if "PartName" in a:
                        a["PartName"] = key(a["PartName"].lstrip("/"))
                    items.append((etree.QName(e).localname, tuple(sorted(a.items()))))
                out[name] = digest(repr(sorted(items)).encode())
            elif low.endswith((".xml", ".vml")):
                out[key(name)] = digest(c14n(parse_xml(blob)))
            else:
                out[key(name)] = digest(blob)
        except etree.XMLSyntaxError:
            out[key(name)] = digest(blob)
    return out, reachable


@dataclass
class PartRules:
    """Which package parts an edit may change / add / remove (keys or prefixes)."""
    changed: set[str] = field(default_factory=set)
    added_prefixes: set[str] = field(default_factory=set)
    removed: set[str] = field(default_factory=set)

    @staticmethod
    def _hit(k: str, pats: Iterable[str]) -> bool:
        return any(k == p or (p.endswith("/") and k.startswith(p)) for p in pats)

    def may_change(self, k: str) -> bool:
        return self._hit(k, self.changed)

    def may_add(self, k: str) -> bool:
        return self._hit(k, self.added_prefixes) or self._hit(k, self.changed)

    def may_remove(self, k: str) -> bool:
        return self._hit(k, self.removed) or self._hit(k, self.changed)


def check_package(before: bytes, after: bytes, rules: PartRules,
                  rename_fn: Callable[[dict[str, bytes]], dict[str, str]] = lambda p: {}) -> list[str]:
    """Human-readable list of parts that changed without permission."""
    bp, ap = read_zip(before), read_zip(after)
    b, b_reach = canon_parts(bp, rename_fn(bp))
    a, _ = canon_parts(ap, rename_fn(ap))
    problems = []
    for k, d in b.items():
        if k.startswith("docProps/"):
            continue  # metadata (app/core props) may be refreshed on save
        if k not in a:
            # a library may drop parts nothing referenced; that loses nothing
            if k in b_reach and not rules.may_remove(k) and not k.endswith(":rels"):
                problems.append(f"removed {k}")
        elif a[k] != d and not rules.may_change(k):
            problems.append(f"changed {k}")
    for k in a:
        if k not in b and not k.startswith("docProps/") and not rules.may_add(k):
            problems.append(f"added {k}")
    return problems


# ------------------------------------------------------------------
# Element-level verification
# ------------------------------------------------------------------

class Snapshot:
    """Fingerprints of addressable objects, tracked by identity across the edit."""

    def __init__(self, fp: Callable[[Any], str]):
        self._fp = fp
        self._before: dict[int, tuple[str, Any, str]] = {}
        self._touched: set[int] = set()
        # lxml proxies are recreated (new id) once unreferenced: keep them alive
        self._keep: list = []

    def take(self, items: Iterable[tuple[str, Any]]) -> None:
        for label, obj in items:
            self._before[id(obj)] = (label, obj, self._fp(obj))

    def touch(self, obj, ancestors: bool = True) -> None:
        """Mark obj (and, for lxml elements, every ancestor) as intentionally edited."""
        self._touched.add(id(obj))
        self._keep.append(obj)
        if ancestors and hasattr(obj, "getparent"):
            p = obj.getparent()
            while p is not None:
                self._touched.add(id(p))
                self._keep.append(p)
                p = p.getparent()

    def is_touched(self, obj) -> bool:
        return id(obj) in self._touched

    def problems(self, items_after: Iterable[tuple[str, Any]]) -> list[str]:
        after = {id(o): (label, o) for label, o in items_after}
        out = []
        for oid, (label, obj, fp) in self._before.items():
            if oid not in after:
                if oid not in self._touched:
                    out.append(f"removed {label}")
            elif oid not in self._touched and self._fp(obj) != fp:
                out.append(f"changed {label}")
        for oid, (label, _o) in after.items():
            if oid not in self._before and oid not in self._touched:
                out.append(f"added {label}")
        return out


def xml_fp(elem) -> str:
    return digest(c14n(elem))


@dataclass
class EditResult:
    data: bytes
    changes: list[str] = field(default_factory=list)   # human summary lines
    notes: list[str] = field(default_factory=list)     # warnings for the user


def require(cond: bool, msg: str) -> None:
    if not cond:
        raise DocOpError(msg)


def op_arg(op: dict, *names: str, default: Any = None, required: bool = True):
    for n in names:
        if n in op and op[n] is not None:
            return op[n]
    if required and default is None:
        raise DocOpError(f"op '{op.get('op')}' needs '{names[0]}'")
    return default
