import re
from typing import Optional
from ..base import require

S_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
RT_TABLE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/table"
RT_CALC = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/calcChain"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

MAX_ROWS_SHOWN = 300
MAX_CELLS_SHOWN = 4000
MAX_CELLS_SCANNED = 1_000_000
# parts openpyxl cannot round-trip; structural edits are refused when present
LOSSY_PARTS = (
    "xl/drawings/", "xl/charts/", "xl/media/", "xl/pivotTables/", "xl/pivotCache/",
    "xl/externalLinks/", "xl/slicers/", "xl/slicerCaches/", "xl/timelines/",
    "xl/threadedComments/", "xl/activeX/", "xl/ctrlProps/", "xl/chartsheets/",
)

_NUM = re.compile(r"^-?\d+(\.\d+)?([eE][-+]?\d+)?$")
# Thousands separators: 1,234 / 1,234,567.89
_THOUSANDS = re.compile(r"^[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?$")
_MAX_NUMERIC_DIGITS = 15
_REF = re.compile(r"^\$?([A-Za-z]{1,3})\$?(\d+)$")


def _s(tag: str) -> str:
    return f"{{{S_NS}}}{tag}"


def col_to_num(col: str) -> int:
    n = 0
    for ch in col.upper():
        n = n * 26 + (ord(ch) - 64)
    return n


def num_to_col(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def split_ref(ref: str) -> tuple[int, int]:
    m = _REF.match(ref.strip())
    require(bool(m), f"bad cell reference '{ref}'")
    return int(m.group(2)), col_to_num(m.group(1))


def _split_addr(addr: str) -> tuple[Optional[str], str]:
    addr = str(addr).strip()
    if "!" in addr:
        sh, ref = addr.rsplit("!", 1)
        sh = sh.strip()
        if sh.startswith("'") and sh.endswith("'"):
            sh = sh[1:-1].replace("''", "'")
        return sh, ref.strip()
    return None, addr


def _range_bounds(ref: str) -> tuple[int, int, int, int]:
    a, _, b = (ref or "A1").partition(":")
    r1, c1 = split_ref(a)
    r2, c2 = split_ref(b) if b else (r1, c1)
    return r1, c1, r2, c2


def _fmt(v) -> str:
    if v is None:
        return ""
    s = str(v)
    return s if len(s) <= 80 else s[:80] + "..."
