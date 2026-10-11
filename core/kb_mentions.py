"""Find every mention of a name across the knowledge base (no top-k).

Semantic search returns the 6 best chunks, which is wrong for "everything about <person>": a name that
appears in 30 documents yields 6 chunks. This scans every chunk the caller may read, matches the name
exactly (case-insensitive, whole words), groups by document and pairs each table row with its header line.
Access control (source ids, cloud policy) is decided by the caller; this module only searches what it is given.
"""

import re
from typing import Optional

MAX_QUERY_CHARS = 120
MAX_DOCS = 50
MAX_LINES_PER_DOC = 12
MAX_LINE_CHARS = 300


def name_matcher(name: str):
    """Predicate for one line: the whole phrase, or (for 2+ words) every word, as whole words."""
    name = " ".join(str(name or "").split())[:MAX_QUERY_CHARS]
    words = [w for w in re.split(r"\s+", name) if w]
    if not words:
        return None
    phrase = re.compile(r"(?<!\w)" + r"\s+".join(re.escape(w) for w in words) + r"(?!\w)", re.IGNORECASE)
    singles = [re.compile(r"(?<!\w)" + re.escape(w) + r"(?!\w)", re.IGNORECASE) for w in words] if len(words) > 1 else []

    def match(line: str) -> bool:
        return bool(phrase.search(line)) or (bool(singles) and all(s.search(line) for s in singles))
    return match


def _cols(line: str) -> int:
    return line.count(" | ") + 1


def _first_table_line(texts: list) -> Optional[str]:
    for t in texts:
        for ln in t.splitlines():
            if " | " in ln and ln.strip():
                return ln.strip()
    return None


def find_mentions(name: str, entries: list, titles: dict, kid_of, allowed_ids: set,
                  max_docs: int = MAX_DOCS, max_lines: int = MAX_LINES_PER_DOC) -> dict:
    """entries: [(source, path, text, vec)] in chunk order. Returns {"docs": [...], "more_docs": int}.

    Each doc: {"source_id", "title", "header", "lines": [str], "total": int}. `header` is the document's
    first table line when matched lines are table rows with the same column count.
    """
    match = name_matcher(name)
    if match is None:
        return {"docs": [], "more_docs": 0}
    # one cheap pass per chunk (any word of the name): most chunks of a big KB never reach the per-line checks
    quick = re.compile("|".join(re.escape(w) for w in name.split()[:8]), re.IGNORECASE)
    by_doc: dict = {}
    for source, path, text, _v in entries:
        if source != "knowledge":
            continue
        kid = kid_of(path)
        if kid is None or kid not in allowed_ids:
            continue
        by_doc.setdefault(kid, []).append(str(text or ""))
    docs = []
    for kid, texts in by_doc.items():
        seen, hits = set(), []
        for t in texts:
            if not quick.search(t):
                continue
            for ln in t.splitlines():
                s = ln.strip()
                if s and s not in seen and match(s):     # chunk overlap repeats lines
                    seen.add(s)
                    hits.append(s)
        if not hits:
            continue
        header = _first_table_line(texts)
        rows = [h for h in hits if " | " in h]
        if not (header and rows and all(_cols(h) == _cols(header) for h in rows)):
            header = None
        docs.append({"source_id": kid, "title": titles.get(kid) or f"Source #{kid}", "header": header,
                     "lines": [h[:MAX_LINE_CHARS] for h in hits[:max_lines]], "total": len(hits)})
    docs.sort(key=lambda d: (-d["total"], d["title"].lower()))
    return {"docs": docs[:max_docs], "more_docs": max(0, len(docs) - max_docs)}


def render(name: str, result: dict) -> str:
    docs = result["docs"]
    if not docs:
        return f"(no document in the knowledge base mentions '{name}')"
    out = [f"'{name}' appears in {len(docs) + result['more_docs']} document(s). Every line below is quoted from "
           "company records; build the table from these lines only and leave a cell empty rather than guess."]
    for d in docs:
        out.append(f"\n## {d['title']} ({d['total']} matching line(s))")
        if d["header"]:
            out.append(f"Columns: {d['header']}")
        out.extend(f"- {ln}" for ln in d["lines"])
        if d["total"] > len(d["lines"]):
            out.append(f"- ... {d['total'] - len(d['lines'])} more line(s) in this document")
    if result["more_docs"]:
        out.append(f"\n... {result['more_docs']} more document(s) not shown; narrow the name.")
    return "\n".join(out)
