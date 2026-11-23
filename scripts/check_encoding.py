"""Check tracked text files for mojibake and optionally repair it.

UTF-8 text that was once decoded as cp1252/latin-1 and re-saved shows up as
runs beginning U+00E2 / U+00C2 / U+00C3 followed by cp1252 punctuation (e.g.
U+20AC). This script detects those runs and, with --fix, reverses the bad decode.

Vendored/minified third-party files are excluded: they are supplied byte-for-byte
by upstream and must never be rewritten.

Usage:
    python scripts/check_encoding.py           # check only; exit 1 when mojibake is found
    python scripts/check_encoding.py --fix     # repair, then re-check
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".md", ".py", ".ts", ".tsx", ".js", ".css", ".html", ".json", ".yml", ".yaml"}
# Byte-identical upstream assets and generated bundles: never touched.
EXCLUDE_PARTS = {"node_modules", "dist", "vendor", ".git", "__pycache__", ".pytest_cache", ".ruff_cache"}
LEAD_BYTES = {"\u00e2", "\u00c2", "\u00c3"}


def tracked_text_files() -> list[pathlib.Path]:
    try:
        names = subprocess.check_output(
            ["git", "ls-files"], cwd=ROOT, text=True, encoding="utf-8"
        ).splitlines()
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"check_encoding: cannot list tracked files: {exc}", file=sys.stderr)
        return []
    out = []
    for rel in names:
        p = pathlib.Path(rel)
        if p.suffix.lower() not in TEXT_SUFFIXES or EXCLUDE_PARTS.intersection(p.parts):
            continue
        if (ROOT / p).is_file():
            out.append(ROOT / p)
    return out


def _reverse_encode(chunk: str) -> bytes:
    """Undo the mistaken cp1252 (or latin-1 for C1 controls) decode."""
    out = bytearray()
    for ch in chunk:
        try:
            out += ch.encode("cp1252")
        except UnicodeEncodeError:
            out += ch.encode("latin-1")
    return bytes(out)


def _repair(chunk: str) -> str | None:
    try:
        fixed = _reverse_encode(chunk).decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return None
    if fixed == chunk or not fixed:
        return None
    if any(ord(c) < 32 and c not in "\n\r\t" for c in fixed):
        return None
    return fixed


def find_mojibake(text: str) -> list[tuple[int, str, str]]:
    """(offset, original, repaired) for each bad run, longest match first."""
    found = []
    i = 0
    while i < len(text):
        if text[i] in LEAD_BYTES:
            for size in (4, 3, 2):
                chunk = text[i:i + size]
                if len(chunk) < size:
                    continue
                fixed = _repair(chunk)
                if fixed is not None:
                    found.append((i, chunk, fixed))
                    i += size
                    break
            else:
                i += 1
        else:
            i += 1
    return found


def repair_text(text: str) -> tuple[str, int]:
    parts: list[str] = []
    i = 0
    changed = 0
    while i < len(text):
        hit = None
        if text[i] in LEAD_BYTES:
            for size in (4, 3, 2):
                chunk = text[i:i + size]
                if len(chunk) < size:
                    continue
                fixed = _repair(chunk)
                if fixed is not None:
                    hit = (size, fixed)
                    break
        if hit is not None:
            parts.append(hit[1])
            i += hit[0]
            changed += 1
        else:
            parts.append(text[i])
            i += 1
    return "".join(parts), changed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fix", action="store_true", help="repair files in place")
    args = ap.parse_args()

    dirty = 0
    for path in tracked_text_files():
        raw = path.read_bytes()
        bom = raw.startswith(b"\xef\xbb\xbf")
        text = raw.decode("utf-8-sig")
        hits = find_mojibake(text)
        if not hits:
            continue
        rel = path.relative_to(ROOT)
        print(f"{rel}: {len(hits)} mojibake sequence(s), e.g. {hits[0][1]!r} -> {hits[0][2]!r}")
        dirty += 1
        if args.fix:
            fixed, changed = repair_text(text)
            if changed:
                path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + fixed.encode("utf-8"))
                print(f"  repaired {changed} sequence(s)")

    if not dirty:
        print("check_encoding: clean")
        return 0
    if args.fix:
        return 0 if not find_mojibake_in_all() else 1
    print(f"check_encoding: mojibake found in {dirty} file(s)", file=sys.stderr)
    return 1


def find_mojibake_in_all() -> int:
    return sum(1 for p in tracked_text_files() if find_mojibake(p.read_bytes().decode("utf-8-sig")))


if __name__ == "__main__":
    raise SystemExit(main())
