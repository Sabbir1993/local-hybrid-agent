import csv
import io
import re
from collections import Counter

_MD_SEP_RE = re.compile(r"^\|?[\s\-:|]+\|?$")

# fence languages that are code, not tabular data: a ```python block must never
# become spreadsheet rows (one code line per row)
_CODE_FENCE_LANGS = frozenset({
    "python", "py", "javascript", "js", "jsx", "typescript", "ts", "tsx",
    "bash", "sh", "shell", "powershell", "ps1", "zsh", "json", "xml", "html",
    "css", "yaml", "yml", "toml", "ini", "c", "cpp", "c++", "h", "java", "go",
    "rust", "ruby", "php", "lua", "diff", "console", "terminal",
})
_CODE_LINE_RE = re.compile(
    r"^\s*(?:def\s+\w+\s*\(|class\s+\w+|import\s+\w|from\s+\S+\s+import\s|"
    r"for\s+\S+\s+in\s|while\s+\S+\s*:|return\s+\S|print\s*\(|#include|"
    r"function\s+\w+\s*\(|const\s+\w+\s*=|let\s+\w+\s*=|require\s*\()", re.IGNORECASE)


def _fenced_blocks(content: str) -> list:
    """(language, body) for every ```-fenced block, in document order."""
    blocks, lines, i = [], content.splitlines(), 0
    while i < len(lines):
        m = re.match(r"^\s*```(.*)$", lines[i])
        if not m:
            i += 1
            continue
        lang = m.group(1).strip().lower()
        i += 1
        body = []
        while i < len(lines) and not re.match(r"^\s*```\s*$", lines[i]):
            body.append(lines[i])
            i += 1
        blocks.append((lang, "\n".join(body)))
        i += 1
    return blocks


def _looks_like_code(body: str) -> bool:
    hits = sum(1 for l in body.splitlines() if _CODE_LINE_RE.match(l))
    return hits >= 2


def _markdown_table_rows(lines: list) -> list:
    """The first real markdown table (header + |---| separator + rows) in `lines`.
    Prose around it is not data, and neither is the separator row itself."""
    for i in range(len(lines) - 1):
        if "|" not in lines[i] or not _MD_SEP_RE.match(lines[i + 1]):
            continue
        rows = []
        for l in lines[i:]:
            if _MD_SEP_RE.match(l):
                continue
            if "|" not in l:
                break                      # the table ends at the first non-pipe line
            cells = [c.strip() for c in l.split("|")]
            if l.startswith("|") and cells and cells[0] == "":
                cells.pop(0)
            if l.endswith("|") and cells and cells[-1] == "":
                cells.pop()
            if any(cells):
                rows.append(cells)
        if len(rows) >= 2 and len(rows[0]) > 1:
            return rows
    return []


def _split_delimited(text: str, delim: str) -> list:
    try:
        return [[c.strip() for c in row]
                for row in csv.reader(io.StringIO(text), delimiter=delim)
                if any(c.strip() for c in row)]
    except Exception:
        return []


def _modal_column_rows(rows: list, min_cols: int = 2) -> list:
    """Rows sharing the most common column count (>= min_cols), in order. Lines
    that do not split the same way - surrounding prose - are dropped."""
    tallies = Counter(len(r) for r in rows if len(r) >= min_cols)
    if not tallies:
        return []
    modal = tallies.most_common(1)[0][0]
    return [r for r in rows if len(r) == modal]


def _sniff_delimited(lines: list) -> list:
    """Best rows over comma / tab / semicolon / pipe, or [] when no delimiter
    turns at least two lines into a consistent multi-column table."""
    best: list = []
    for delim in (",", "\t", ";", "|"):
        rows = _modal_column_rows(_split_delimited("\n".join(lines), delim))
        if len(rows) >= 2 and len(rows) > len(best):
            best = rows
    return best


def _parse_tabular_text(content: str) -> list[list[str]]:
    """Spreadsheet rows taken from model output: the table, never the prose
    around it.

    Order: a fenced data block, then a markdown table anywhere in the reply, then
    delimited text (sniffed, and filtered to the rows that split consistently so
    surrounding sentences drop out), and only when nothing multi-column exists,
    every line as a single column.
    """
    lines = [l.strip() for l in content.strip().splitlines() if l.strip()]
    if not lines:
        return []
    blocks = _fenced_blocks(content)

    # 1. a ```csv / ```markdown / bare ``` data block - but never a code block
    for lang, body in blocks:
        if lang in _CODE_FENCE_LANGS or not body.strip() or _looks_like_code(body):
            continue
        body_lines = [l.strip() for l in body.splitlines() if l.strip()]
        rows = _markdown_table_rows(body_lines) or _sniff_delimited(body_lines)
        if rows:
            return rows

    # 2. a markdown table with a |---|---| separator, wherever it appears
    rows = _markdown_table_rows(lines)
    if rows:
        return rows

    # 3. delimited text: rows that do not split the same way are prose
    rows = _sniff_delimited(lines)
    if rows:
        return rows

    # 4. nothing multi-column: one column of lines, minus fence markers. A reply
    #    whose only body is code is an answer, not data - return [] so the caller
    #    reports "no tabular data" instead of saving one code line per row.
    if any(lang in _CODE_FENCE_LANGS or _looks_like_code(body) for lang, body in blocks):
        return []
    return [[l] for l in lines if not l.startswith("```")]
