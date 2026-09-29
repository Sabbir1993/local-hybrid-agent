from ..base import DocOpError
from .constants import (
    _MAX_NUMERIC_DIGITS,
    _NUM,
    _THOUSANDS,
)


def _string_to_kind(original: str, t: str) -> tuple:
    """Number rules for a stripped string. `original` is what gets stored, so a
    value refused as a number is never rewritten on the way in."""
    if not t:
        return "str", original
    signless = t[1:] if t[:1] in "+-" else t
    intpart = signless.split(".", 1)[0]
    # An integer part with a leading zero is an identifier, not a number: 007,
    # 01234 and 02134 are codes/ZIPs and must keep their zeros.
    if len(intpart) > 1 and intpart.startswith("0"):
        return "str", original
    if intpart.isdigit() and len(intpart) > _MAX_NUMERIC_DIGITS:
        return "str", original
    if _THOUSANDS.match(t):
        plain = t.replace(",", "")
        return "num", float(plain) if any(ch in plain for ch in ".eE") else int(plain)
    if _NUM.match(t):
        return "num", float(t) if any(ch in t for ch in ".eE") else int(t)
    return "str", original


def _coerce(value, allow_formula: bool):
    """(kind, payload) for a cell value from the model.

    The rule is "keep what the model wrote unless it is unambiguously a number":
    identifiers keep their leading zeros and their length, and a currency symbol
    is formatting rather than data. Dates stay as written - ISO text still sorts
    chronologically, whereas a serial number would need a cell style this path
    does not own."""
    if value is None or value == "":
        return "empty", None
    if isinstance(value, bool):
        return "bool", value
    if isinstance(value, (int, float)):
        return "num", value
    s = str(value)
    if s.startswith("="):
        if not allow_formula:
            raise DocOpError(f"value '{s[:40]}' looks like a formula - use set_formula "
                             "(or formulas: true in set_range) to write formulas")
        return "formula", s[1:]
    t = s.strip()
    if len(t) > 1 and t[0] in "€£¥$":
        t = t[1:].strip()
        if not t:
            return "str", s
    return _string_to_kind(s, t)
