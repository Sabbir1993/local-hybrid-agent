"""Keep the complete part of a write call that was cut at the size cap.

The stream guard (routes/common/sse_stream.py) stops a write_file/append_file once its content passes the per-call cap
and hands the tool what was generated so far as `_partial`. Throwing that away cost a local model ~10k tokens per try,
and it showed the model its own call with empty content, so it tried the same huge section again (and again).

Here the longest prefix that is safe to save is kept:
  - code the server can parse (Python, JS/TS, JSON, YAML, TOML, XML): cut before a top-level line, and only where the
    whole file still parses
  - other code (PHP, Go, CSS, shell ...): cut before a top-level line that follows a blank line, i.e. between blocks
  - plain text (.md, .txt, .csv, unknown): cut at the last complete line
Nothing is saved when no such prefix exists (one giant function), and the caller falls back to the size error.
"""
from pathlib import PurePath
from typing import Optional

from . import edit_engine

# validate_code_syntax really parses these (tree-sitter ones pass when the parser is missing, like everywhere else)
PARSED_EXTS = frozenset({".py", ".pyw", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".mts", ".cts", ".tsx",
                         ".json", ".toml", ".yaml", ".yml", ".xml", ".svg"})
# code the server cannot parse: a cut mid-block would leave a half statement, so only cut between blocks
BLOCK_EXTS = frozenset({".php", ".go", ".rs", ".java", ".kt", ".kts", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs",
                        ".rb", ".sh", ".bash", ".ps1", ".psm1", ".bat", ".cmd", ".css", ".scss", ".less", ".sql",
                        ".swift", ".lua", ".dart", ".scala", ".r", ".pl", ".vue", ".svelte"})
MIN_SAVED_LINES = 3        # less than this is not worth a "partly saved" round
MAX_TRIES = 40             # parse attempts per salvage, longest prefix first


def _top_level_cuts(lines: list, need_blank_before: bool) -> list:
    """Character offsets (into ''.join(lines)) where a top-level line starts, longest prefix first."""
    cuts, pos = [], 0
    offs = []
    for ln in lines:
        offs.append(pos)
        pos += len(ln)
    for i in range(1, len(lines)):
        ln = lines[i]
        if not ln.strip() or ln[0] in " \t":
            continue
        if need_blank_before and lines[i - 1].strip():
            continue
        cuts.append(offs[i])
    cuts.reverse()
    return cuts


def salvage_prefix(before: Optional[str], partial: str, path: str) -> str:
    """The longest leading part of `partial` that can be appended to `before` (None/'' for a new file) without leaving
    broken or half-written code. '' when there is none."""
    if not partial or "\n" not in partial:
        return ""
    whole = partial[:partial.rindex("\n") + 1]            # the last line is cut mid-way: never keep it
    lines = whole.splitlines(keepends=True)
    ext = PurePath(path).suffix.lower()

    if ext in PARSED_EXTS:
        from .verify import validate_code_syntax
        cands = [len(whole)] + _top_level_cuts(lines, need_blank_before=False)
        for cut in cands[:MAX_TRIES]:
            pre = whole[:cut]
            if pre.count("\n") < MIN_SAVED_LINES:
                break
            ok, _ = validate_code_syntax(path, edit_engine.append_text(before, pre))
            if ok:
                return pre
        return ""
    if ext in BLOCK_EXTS:
        cuts = _top_level_cuts(lines, need_blank_before=True)
        pre = whole[:cuts[0]] if cuts else ""
        return pre if pre.count("\n") >= MIN_SAVED_LINES else ""
    return whole if whole.count("\n") >= MIN_SAVED_LINES else ""


def last_line(text: str) -> str:
    for ln in reversed(text.rstrip().splitlines()):
        if ln.strip():
            return ln.strip()[:80]
    return ""
