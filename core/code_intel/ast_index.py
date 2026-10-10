"""core/code_intel/ast_index.py - tree-sitter symbol index (R9).

grep answers "where is this string"; the agent needs "where is this SYMBOL
defined, what calls it, what does this file contain" - cross-file, with line
numbers, without reading bodies into context. Pure functions over a repo
root; no server, no network, no model. Python + JavaScript + TypeScript/TSX
grammars (tree-sitter-python/-javascript/-typescript, hash-locked). TS reuses
the JS walker (the TS grammar is a superset of JS node names); see tests/
fixtures/code_repo/types.ts + widget.tsx and the eval code slice for coverage.

Scope honesty: callers are syntactic call sites (identifier/attribute match),
not type-resolved references. A rename pair, overloads-by-module, and dynamic
dispatch will show up as text does - this is structure-aware grep, and the
slice measures exactly that, not an IDE.
"""

import os
import time
from pathlib import Path
from typing import Optional

import tree_sitter
import tree_sitter_javascript as _tsjs
import tree_sitter_python as _tspy
import tree_sitter_typescript as _tsts

_PY = tree_sitter.Language(_tspy.language())
_JS = tree_sitter.Language(_tsjs.language())
_TS = tree_sitter.Language(_tsts.language_typescript())
_TSX = tree_sitter.Language(_tsts.language_tsx())

_LANGS = {".py": _PY, ".js": _JS, ".jsx": _JS, ".mjs": _JS, ".cjs": _JS}
_LANGS[".ts"] = _TS
_LANGS[".tsx"] = _TSX
_SKIPPED_SUFFIXES = set()  # py, js, ts, tsx all have locked grammars now

_PARSERS = {}


def _parser(lang) -> tree_sitter.Parser:
    p = _PARSERS.get(id(lang))
    if p is None:
        p = tree_sitter.Parser(lang)
        _PARSERS[id(lang)] = p
    return p


def _text(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _named(node, *kinds) -> list:
    return [c for c in node.children if c.is_named and (not kinds or c.type in kinds)]


def _name_of(node, src: bytes) -> str:
    for c in node.children:
        # JS calls the class/method name node `identifier`; TS calls the class
        # name `type_identifier` and method names `property_identifier`. The
        # extra names do not occur in the JS grammar, so matching them changes
        # nothing for .js/.jsx while making .ts/.tsx extract class + method
        # names instead of empty strings.
        if c.type in ("identifier", "type_identifier", "property_identifier"):
            return _text(c, src)
    return ""


def _docstring_of(body, src: bytes) -> str:
    kids = _named(body)
    if not kids or kids[0].type != "expression_statement":
        return ""
    expr = _named(kids[0])
    if len(expr) == 1 and expr[0].type == "string":
        raw = _text(expr[0], src).strip()
        for q in ('"""', "'''", '"', "'"):
            if raw.startswith(q) and raw.endswith(q) and len(raw) >= 2 * len(q):
                return raw[len(q):-len(q)].strip()
    return ""


def _walk_py(root, src: bytes, rel: str, syms: list, calls: list) -> None:
    def visit(node, klass: str = "", enclosing: str = ""):
        if node.type == "function_definition":
            name = _name_of(node, src)
            fn_sym = f"{klass}.{name}" if klass else name
            params = next((c for c in node.children if c.type == "parameters"), None)
            body = next((c for c in node.children if c.type == "block"), None)
            syms.append({
                "name": name, "kind": "method" if klass else "function",
                "class": klass or None, "file": rel,
                "line": node.start_point[0] + 1,
                "end_line": node.end_point[0] + 1,
                "signature": _text(params, src) if params is not None else "()",
                "docstring": _docstring_of(body, src) if body is not None else "",
            })
            if body is not None:
                for c in node.children:
                    visit(c, klass=klass, enclosing=fn_sym)
            return
        elif node.type == "class_definition":
            name = _name_of(node, src)
            body = next((c for c in node.children if c.type == "block"), None)
            syms.append({
                "name": name, "kind": "class", "class": None, "file": rel,
                "line": node.start_point[0] + 1,
                "end_line": node.end_point[0] + 1,
                "signature": "",
                "docstring": _docstring_of(body, src) if body is not None else "",
            })
            if body is not None:
                for c in _named(body):
                    visit(c, klass=name, enclosing=enclosing)
            return
        elif node.type == "call":
            fn = next((c for c in node.children
                       if c.type in ("identifier", "attribute")), None)
            if fn is not None:
                if fn.type == "identifier":
                    called = _text(fn, src)
                else:
                    attr = fn.child_by_field_name("attribute")
                    called = _text(attr, src) if attr is not None else ""
                if called:
                    calls.append({
                        "name": called,
                        "file": rel,
                        "line": node.start_point[0] + 1,
                        "caller": enclosing or None,
                    })
        for c in node.children:
            visit(c, klass=klass, enclosing=enclosing)

    visit(root)


def _walk_js(root, src: bytes, rel: str, syms: list, calls: list) -> None:
    def _record_fn(node, name: str, klass: str = "", kind: str = "function"):
        params = next((c for c in node.children if c.type == "formal_parameters"), None)
        syms.append({
            "name": name, "kind": "method" if klass else kind,
            "class": klass or None, "file": rel,
            "line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "signature": _text(params, src) if params is not None else "()",
            "docstring": "",
        })

    def visit(node, klass: str = "", enclosing: str = ""):
        if node.type in ("function_declaration", "generator_function_declaration"):
            name = _name_of(node, src)
            _record_fn(node, name, klass=klass)
            for c in node.children:
                visit(c, klass=klass, enclosing=name)
            return
        elif node.type == "class_declaration":
            name = _name_of(node, src)
            body = next((c for c in node.children if c.type == "class_body"), None)
            syms.append({
                "name": name, "kind": "class", "class": None, "file": rel,
                "line": node.start_point[0] + 1,
                "end_line": node.end_point[0] + 1,
                "signature": "", "docstring": "",
            })
            if body is not None:
                for c in _named(body):
                    visit(c, klass=name, enclosing=enclosing)
            return
        elif node.type == "method_definition":
            name = _name_of(node, src)
            fn_sym = f"{klass}.{name}" if klass else name
            _record_fn(node, name, klass=klass)
            for c in node.children:
                visit(c, klass=klass, enclosing=fn_sym)
            return
        elif node.type == "variable_declarator":
            val = node.child_by_field_name("value")
            if val is not None and val.type in ("arrow_function", "function_expression",
                                                "function"):
                name = _name_of(node, src)
                _record_fn(val, name, klass=klass)
                for c in val.children:
                    visit(c, klass=klass, enclosing=name)
                return
        elif node.type == "call_expression":
            fn = node.child_by_field_name("function")
            called = ""
            if fn is not None:
                if fn.type == "identifier":
                    called = _text(fn, src)
                elif fn.type == "member_expression":
                    prop = fn.child_by_field_name("property")
                    called = _text(prop, src) if prop is not None else ""
            if called:
                calls.append({
                    "name": called,
                    "file": rel,
                    "line": node.start_point[0] + 1,
                    "caller": enclosing or None,
                })
        for c in node.children:
            visit(c, klass=klass, enclosing=enclosing)

    visit(root)


SUPPORTED_SUFFIXES = frozenset(_LANGS)


def parse_source(rel: str, src: bytes) -> tuple:
    """Parse one file's bytes. Returns (status, symbols, calls); status is "ok", "unsupported" or
    "parse_error". Pure: no disk, so the same code serves a local root and sources fetched from a device."""
    lang = _LANGS.get(os.path.splitext(rel)[1].lower())
    if lang is None:
        return "unsupported", [], []
    tree = _parser(lang).parse(src)
    if tree.root_node.has_error:
        return "parse_error", [], []
    syms, calls = [], []
    if lang is _PY:
        _walk_py(tree.root_node, src, rel, syms, calls)
    else:
        _walk_js(tree.root_node, src, rel, syms, calls)
    return "ok", syms, calls


class SourceIndex:
    """Incremental in-memory index over sources that arrive as text (fetched from the user's device).

    Per-file results are kept with the stamp they were parsed at, so a refresh re-parses only files whose
    stamp changed. Nothing touches disk: the server never holds the user's workspace.
    """

    def __init__(self):
        self._files: dict = {}          # rel -> {"stamp": tuple, "status": str, "symbols": [], "calls": []}
        self._data = None

    def stamps(self) -> dict:
        return {rel: f["stamp"] for rel, f in self._files.items()}

    def update(self, rel: str, stamp, src: bytes) -> None:
        status, syms, calls = parse_source(rel, src)
        self._files[rel] = {"stamp": tuple(stamp), "status": status, "symbols": syms, "calls": calls}
        self._data = None

    def remove(self, rel: str) -> None:
        if self._files.pop(rel, None) is not None:
            self._data = None

    def retain(self, keep) -> None:
        """Drop every file not in `keep` (deleted on the device)."""
        keep = set(keep)
        for rel in [r for r in self._files if r not in keep]:
            self.remove(rel)

    def data(self) -> dict:
        if self._data is None:
            syms, calls, skipped, files = [], [], [], 0
            for rel in sorted(self._files):
                f = self._files[rel]
                if f["status"] == "ok":
                    files += 1
                    syms += f["symbols"]
                    calls += f["calls"]
                elif f["status"] == "parse_error":
                    skipped.append(rel + " (parse error)")
            syms.sort(key=lambda s: (s["file"], s["line"]))
            calls.sort(key=lambda c: (c["file"], c["line"]))
            self._data = {"symbols": syms, "calls": calls, "files": files, "skipped": skipped}
        return self._data

    def outline(self, rel: str) -> Optional[list]:
        """Skeleton of one indexed file, or None when it is not indexed (unknown or unparseable)."""
        f = self._files.get(rel)
        if f is None or f["status"] != "ok":
            return None
        return [{"kind": s["kind"], "name": s["name"], "class": s["class"], "line": s["line"],
                 "end_line": s["end_line"], "signature": s["signature"], "docstring": s["docstring"]}
                for s in f["symbols"]]

    def status(self, rel: str) -> Optional[str]:
        f = self._files.get(rel)
        return f["status"] if f else None


def _repo_stamp(root) -> dict:
    """{rel_posix: (mtime_ns, size)} for every parseable or skipped-suffix file.

    The stamp IS the invalidation key: additions, deletions, edits, and
    suffix-class changes (py -> skipped ts) all alter it. Non-code files are
    skipped before stat (they can't change what the index contains), and
    os.scandir (FindFirstFile on Windows) reports dir-ness for free - the
    whole walk is ~4ms for 1,000 files vs ~35ms for pathlib rglob + stat.
    Symlinked dirs are not followed: no symlink loops, matches rglob bounds.
    """
    stamp = {}
    now_ns = time.time_ns()
    stack = [os.fspath(root)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                            continue
                        suffix = os.path.splitext(e.name)[1].lower()
                        if suffix not in _LANGS and suffix not in _SKIPPED_SUFFIXES:
                            continue
                        # follow file symlinks so target edits invalidate;
                        # symlinked dirs are not traversed (loop safety)
                        st = e.stat()
                        rel = os.path.relpath(e.path, root)
                        key = rel.replace(os.sep, "/")
                        if now_ns - st.st_mtime_ns < _TICK_NS:
                            # too fresh to trust mtime: a same-size rewrite in the
                            # same clock tick is invisible to (mtime_ns, size)
                            try:
                                with open(e.path, "rb") as fh:
                                    digest = hash(fh.read())
                            except OSError:
                                continue
                            stamp[key] = (st.st_mtime_ns, st.st_size, digest)
                        else:
                            stamp[key] = (st.st_mtime_ns, st.st_size)
                    except OSError:
                        continue
        except OSError:
            continue
    return stamp


# root -> {"stamp": {...}, "data": {...}}. In-process cache: a repo answering
# many agent queries (defs, callers, outlines) parses once, not once per
# query. Correctness comes from re-stamping on every call - a stale cache is
# worse than a slow one for code search (tests/test_code_intel_cache.py).
_CACHE: dict = {}

# An edit whose size is unchanged AND whose mtime falls in the same clock tick is
# invisible to (mtime_ns, size): measured on this machine, 25 of 40 consecutive
# same-size rewrites shared an mtime, because the Windows clock resolution is
# 15.6ms. So a file whose stamp is younger than the clock tick is hashed as
# well - the guard costs one read per recently-touched file and closes the hole
# that would otherwise hand the agent a stale index for the file it just edited.
_TICK_NS = int(15.625 * 1e6) * 2


def _sqlite_cache_path(root) -> "Path":
    return Path(root) / ".ast_cache.db"


def _open_sqlite_cache(root) -> "Optional[object]":
    """Return a sqlite3 connection to the persistent AST cache, or None on failure."""
    import sqlite3
    try:
        db_path = _sqlite_cache_path(root)
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS symbols (
                name TEXT, kind TEXT, class_ TEXT, file TEXT,
                line INTEGER, end_line INTEGER,
                signature TEXT, docstring TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS calls (
                name TEXT, file TEXT, line INTEGER, caller TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stats (
                files INTEGER, skipped TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sym_name ON symbols(name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sym_file ON symbols(file)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_call_name ON calls(name)")
        conn.commit()
        return conn
    except Exception:
        return None


def _load_sqlite_cache(conn, stamp: dict) -> "Optional[dict]":
    """Return cached data if the persisted stamp matches, else None."""
    import json
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='stamp'").fetchone()
        if row is None:
            return None
        stored_stamp = json.loads(row[0])
        if stored_stamp != stamp:
            return None
        syms = [
            {"name": r[0], "kind": r[1], "class": r[2], "file": r[3],
             "line": r[4], "end_line": r[5], "signature": r[6], "docstring": r[7]}
            for r in conn.execute(
                "SELECT name, kind, class_, file, line, end_line, signature, docstring FROM symbols"
            ).fetchall()
        ]
        calls = [
            {"name": r[0], "file": r[1], "line": r[2], "caller": r[3]}
            for r in conn.execute("SELECT name, file, line, caller FROM calls").fetchall()
        ]
        stats_row = conn.execute("SELECT files, skipped FROM stats").fetchone()
        files = stats_row[0] if stats_row else 0
        skipped = json.loads(stats_row[1]) if stats_row else []
        return {"symbols": syms, "calls": calls, "files": files, "skipped": skipped}
    except Exception:
        return None


def _write_sqlite_cache(conn, stamp: dict, data: dict) -> None:
    """Persist parsed symbol+call data to SQLite for warm-start on next run."""
    import json
    try:
        conn.execute("DELETE FROM meta")
        conn.execute("DELETE FROM symbols")
        conn.execute("DELETE FROM calls")
        conn.execute("DELETE FROM stats")
        conn.execute("INSERT INTO meta VALUES ('stamp', ?)", (json.dumps(stamp),))
        conn.executemany(
            "INSERT INTO symbols VALUES (?,?,?,?,?,?,?,?)",
            [(s["name"], s["kind"], s.get("class"), s["file"],
              s["line"], s.get("end_line", s["line"]), s.get("signature", ""),
              s.get("docstring", ""))
             for s in data["symbols"]]
        )
        conn.executemany(
            "INSERT INTO calls VALUES (?,?,?,?)",
            [(c["name"], c["file"], c["line"], c.get("caller")) for c in data["calls"]]
        )
        conn.execute("INSERT INTO stats VALUES (?, ?)",
                     (data["files"], json.dumps(data.get("skipped", []))))
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass


def index_repo(root) -> dict:
    """Parse every supported file under root. Returns symbols, calls, stats.

    Cache hierarchy:
    1. Hot in-process dict (_CACHE): microseconds, same Python session.
    2. Persistent SQLite (.ast_cache.db): ~3ms warm-start across sessions /
       server restarts. Written after every cold parse; invalidated when any
       tracked file's (mtime_ns, size) changes.
    3. Cold parse: tree-sitter walks all files; result written to both layers.

    The stamp IS the invalidation key — additions, deletions, and edits all
    alter it. Correctness is identical to the previous in-memory-only path.
    """
    root = Path(root)
    key = str(root)
    stamp = _repo_stamp(root)

    # Layer 1: hot in-process dict
    hit = _CACHE.get(key)
    if hit is not None and hit["stamp"] == stamp:
        return hit["data"]

    # Layer 2: persistent SQLite warm-start
    conn = _open_sqlite_cache(root)
    if conn is not None:
        cached = _load_sqlite_cache(conn, stamp)
        if cached is not None:
            _CACHE[key] = {"stamp": stamp, "data": cached}
            conn.close()
            return cached

    # Layer 3: cold parse
    syms, calls = [], []
    files, skipped = 0, []
    for rel in sorted(stamp):
        path = root / rel
        lang = _LANGS.get(path.suffix.lower())
        if lang is None:
            skipped.append(path.name)
            continue
        try:
            src = path.read_bytes()
        except OSError:
            continue
        tree = _parser(lang).parse(src)
        if tree.root_node.has_error:
            skipped.append(path.name + " (parse error)")
            continue
        files += 1
        if lang is _PY:
            _walk_py(tree.root_node, src, rel, syms, calls)
        else:
            _walk_js(tree.root_node, src, rel, syms, calls)
    syms.sort(key=lambda s: (s["file"], s["line"]))
    calls.sort(key=lambda c: (c["file"], c["line"]))
    data = {"symbols": syms, "calls": calls, "files": files, "skipped": skipped}
    _CACHE[key] = {"stamp": stamp, "data": data}

    # Persist to SQLite for next session warm-start
    if conn is not None:
        _write_sqlite_cache(conn, stamp, data)
        conn.close()

    return data


def definitions_in(data: dict, name: str, path_hint: str = None) -> list:
    """All definitions of `name` in an index `data` dict; path_hint narrows to matching paths.

    The hint matches a full relative path or a path SUFFIX ("pkg/auth.py"),
    never a substring: hint "auth.py" must not match "legacy_auth.py".
    """
    hits = [s for s in data["symbols"] if s["name"] == name]
    if path_hint:
        hits = [h for h in hits
                if h["file"] == path_hint or h["file"].endswith("/" + path_hint)]
    return hits


def callers_in(data: dict, name: str) -> list:
    """Every syntactic call site of `name`, with file, line, and caller function."""
    return [c for c in data["calls"] if c["name"] == name]


def find_symbol_definition(name: str, root, path_hint: str = None) -> list:
    """All definitions of `name` under a local root (see definitions_in)."""
    return definitions_in(index_repo(root), name, path_hint)


def find_symbol_callers(name: str, root) -> list:
    """Every syntactic call site of `name` under a local root."""
    return callers_in(index_repo(root), name)


def find_symbol_callees(name: str, root, path_hint: str = None) -> list:
    """Every symbol called inside the body of `name`.
    Cross-references targets with their definitions across the workspace.
    """
    return callees_in(index_repo(root), name, path_hint)


def callees_in(data: dict, name: str, path_hint: str = None) -> list:
    """find_symbol_callees over an index `data` dict."""
    defs = definitions_in(data, name, path_hint=path_hint)
    if not defs:
        return []
    callees = []
    seen = set()
    for d in defs:
        d_file = d["file"]
        start_l = d["line"]
        end_l = d.get("end_line", start_l)
        for c in data["calls"]:
            if c["file"] == d_file and start_l <= c["line"] <= end_l:
                key = (c["name"], c["file"], c["line"])
                if key in seen:
                    continue
                seen.add(key)
                targets = [t for t in data["symbols"] if t["name"] == c["name"]]
                callees.append({
                    "name": c["name"],
                    "caller": name,
                    "file": d_file,
                    "line": c["line"],
                    "definitions": [{"file": t["file"], "line": t["line"], "kind": t["kind"]} for t in targets],
                })
    return callees


def get_call_hierarchy(name: str, root, path_hint: str = None) -> dict:
    """Directed cross-file call hierarchy for `name`: incoming callers and outgoing callees."""
    incoming = find_symbol_callers(name, root)
    outgoing = find_symbol_callees(name, root, path_hint=path_hint)
    return {
        "symbol": name,
        "incoming_callers": incoming,
        "outgoing_callees": outgoing,
    }


def get_file_outline(path) -> list:
    """Skeleton of one file: classes, functions, methods with signatures.

    Bodies stay out so the agent learns structure without spending context.
    """
    path = Path(path)
    lang = _LANGS.get(path.suffix.lower())
    if lang is None:
        raise ValueError(f"unsupported file type for outline: {path.suffix}")
    src = path.read_bytes()
    syms, calls = [], []
    tree = _parser(lang).parse(src)
    rel = path.name
    if lang is _PY:
        _walk_py(tree.root_node, src, rel, syms, calls)
    else:
        _walk_js(tree.root_node, src, rel, syms, calls)
    return [{"kind": s["kind"], "name": s["name"], "class": s["class"],
             "line": s["line"], "end_line": s["end_line"],
             "signature": s["signature"], "docstring": s["docstring"]}
            for s in syms]

