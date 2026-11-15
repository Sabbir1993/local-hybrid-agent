"""Recover the file a model meant when it retyped a generated file name wrongly (truncated, a character changed).

A Personal Agent's write_file is renamed by the server to '<name>_<8 hex>.<ext>' (execution.unique_file_name), so the
model has to copy a random id it cannot reliably copy. Recovery therefore knows that rename: asking for '<name>.<ext>'
means the newest '<name>_<8 hex>.<ext>' the agent wrote.

Pure functions, no I/O: the caller decides what a match may be used for. Only files the agent itself wrote this
session are candidates, so an unrelated file is never picked."""
import re
from difflib import SequenceMatcher
from pathlib import PurePath
from typing import Optional

MIN_RATIO = 0.85     # name similarity for a changed character or two
MIN_MARGIN = 0.04    # the best fuzzy match must beat the runner-up by this much, else it is ambiguous
MIN_PREFIX = 6       # shortest truncated name that counts as a prefix match
ID_VARIANT = 0.99    # the server's own '<name>_<8 hex>' rename of what was asked for

_ID_TAIL = re.compile(r"(?:[-_][0-9a-f]{8})+")


def _ext(name: str) -> str:
    return name.rsplit(".", 1)[1] if "." in name else ""


def _stem(name: str) -> str:
    e = _ext(name)
    return name[:-len(e) - 1] if e else name


def is_id_variant(asked: str, cand: str) -> bool:
    """cand is asked with a server-added '_<8 hex>' id before the extension ('report.html' -> 'report_1a2b3c4d.html')."""
    a, c = asked.lower(), cand.lower()
    if _ext(a) and _ext(a) != _ext(c):
        return False
    sa, sc = _stem(a), _stem(c)
    return len(sa) > 0 and sc.startswith(sa) and _ID_TAIL.fullmatch(sc[len(sa):]) is not None


def score(asked: str, cand: str) -> float:
    """0..1: how likely `cand` is the file named `asked`."""
    a, c = asked.lower(), cand.lower()
    if a == c:
        return 1.0
    if is_id_variant(a, c):
        return ID_VARIANT
    if _ext(a) and _ext(a) != _ext(c):                   # an explicit extension must agree
        return 0.0
    stem = _stem(a)
    if len(stem) >= MIN_PREFIX and c.startswith(stem):   # cut short, extension dropped
        return 0.95
    return SequenceMatcher(None, a, c).ratio()


def match_written(asked: PurePath, written: list) -> Optional[PurePath]:
    """The one written file in the same folder that `asked` most likely meant, or None (no match or ambiguous).

    `written` is oldest first. Several server renames of the same name are not ambiguous: the newest one wins."""
    parent = str(asked.parent).lower()
    scored, seen = [], set()
    for i, w in enumerate(written):
        wp = type(asked)(w)
        key = str(wp).lower()
        if key in seen or str(wp.parent).lower() != parent:
            continue
        seen.add(key)
        s = score(asked.name, wp.name)
        if s >= MIN_RATIO:
            scored.append((s, i, wp))
    if not scored:
        return None
    top = max(s for s, _, _ in scored)
    if top >= ID_VARIANT:
        return max((t for t in scored if t[0] == top), key=lambda t: t[1])[2]
    scored.sort(key=lambda t: t[0], reverse=True)
    if len(scored) > 1 and scored[0][0] - scored[1][0] < MIN_MARGIN:
        return None
    return scored[0][2]
