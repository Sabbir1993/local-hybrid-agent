"""Recover the file a model meant when it retyped a generated file name wrongly (truncated, a character changed).

Pure functions, no I/O: the caller decides what a match may be used for. Only files the agent itself wrote this
session are candidates, and a match must be unique, so an unrelated file is never picked."""
from difflib import SequenceMatcher
from pathlib import PurePath
from typing import Optional

MIN_RATIO = 0.85     # name similarity for a changed character or two
MIN_MARGIN = 0.04    # the best match must beat the runner-up by this much, else it is ambiguous
MIN_PREFIX = 6       # shortest truncated name that counts as a prefix match


def _ext(name: str) -> str:
    return name.rsplit(".", 1)[1] if "." in name else ""


def score(asked: str, cand: str) -> float:
    """0..1: how likely `cand` is the file named `asked`."""
    a, c = asked.lower(), cand.lower()
    if a == c:
        return 1.0
    if _ext(a) and _ext(a) != _ext(c):                   # an explicit extension must agree
        return 0.0
    stem = a.rsplit(".", 1)[0] if _ext(a) else a
    if len(stem) >= MIN_PREFIX and c.startswith(stem):   # cut short, extension dropped
        return 0.95
    return SequenceMatcher(None, a, c).ratio()


def match_written(asked: PurePath, written: list) -> Optional[PurePath]:
    """The one written file in the same folder that `asked` most likely meant, or None (no match or ambiguous)."""
    parent = str(asked.parent).lower()
    scored, seen = [], set()
    for w in written:
        wp = type(asked)(w)
        key = str(wp).lower()
        if key in seen or str(wp.parent).lower() != parent:
            continue
        seen.add(key)
        s = score(asked.name, wp.name)
        if s >= MIN_RATIO:
            scored.append((s, wp))
    if not scored:
        return None
    scored.sort(key=lambda t: t[0], reverse=True)
    if len(scored) > 1 and scored[0][0] - scored[1][0] < MIN_MARGIN:
        return None
    return scored[0][1]
