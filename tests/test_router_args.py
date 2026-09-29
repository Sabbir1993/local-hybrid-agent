"""Router argument extraction (core/small_model.py::_extract_simple_args).

The CPU routers (Laya / Needle) choose a tool but cannot type its arguments, so
_extract_simple_args re-derives them from the user's sentence with regexes. These
tests pin that behaviour down:

  * FROZEN - extractions that already worked before the extension/glob/grep fix;
    they must stay byte-identical (that is the "nothing broke" gate)
  * FIXED  - the cases the fix repairs, and values that must NOT have moved
  * the invariants the router lane may never violate: a drive letter or ".." in a
    path, a grep pattern re.compile rejects, or a changed return shape

Run: python -m unittest tests.test_router_args -v
"""

import ast
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.small_model import _extract_simple_args, _safe_grep_pattern  # noqa: E402

SRC = Path(__file__).resolve().parents[1] / "core" / "small_model" / "router.py"

# Frozen 2026-09-29 against the pre-fix function, then re-checked against the fix.
FROZEN = (
    ("list_files", "list all *.py files", {"pattern": "*.py"}),
    ("list_files", "show me **/*.json", {"pattern": "**/*.json"}),
    ("list_files", "list files in the project", {}),
    ("list_files", "what files exist", {}),
    ("read_file", 'read "src/app.py" please', {"path": "src/app.py"}),
    ("read_file", "read config/app.json", {"path": "config/app.json"}),
    ("read_file", "read my file named app.py", {"path": "app.py"}),
    ("read_file", "read note.txt", {"path": "note.txt"}),
    # the leading "." / ".." is skipped by the path body class (it excludes ".")
    # and core/agent_tools._ws_resolve strips the separator, so both resolve to
    # the same file as before -- do not "fix" these two rows by accident
    ("read_file", "read ./src/app.py", {"path": "/src/app.py"}),
    ("read_file", "read ../x/y.md", {"path": "/x/y.md"}),
    ("read_file", "read /etc/passwd", {}),
    ("grep", "grep for 'router_route'", {"pattern": "router_route"}),
    ("grep", "find TODO in the repo", {"pattern": "TODO"}),
    ("grep", "grep router_route", {"pattern": "router_route"}),
    ("read_file", "what is the weather", {}),
)

FIXED = (
    # A1: extension no longer cut at 6 chars, and may chain (app.tar.gz)
    ("read_file", "read app.tar.gz", {"path": "app.tar.gz"}),
    # A1: extension must be letter-led, so a bare version number is not a file
    ("read_file", "read version 2.5 report", {}),
    # A3: the closing "*" is kept
    ("list_files", "show *foo* files", {"pattern": "*foo*"}),
    ("list_files", "show *foo files", {"pattern": "*foo"}),
    ("list_files", "list *.py", {"pattern": "*.py"}),
    # A2: a pattern tool_grep can compile passes through untouched, so regexes
    # the user typed on purpose keep working
    ("grep", "grep for 'a+b*c'", {"pattern": "a+b*c"}),
    ("grep", "grep for [a-z]+", {"pattern": "[a-z]+"}),
    ("grep", "look at data (draft)", {"pattern": "look at data (draft)"}),
    # A2: an invalid pattern is escaped instead of costing a step to re.error
    ("grep", "grep for [unclosed", {"pattern": "\\[unclosed"}),
    ("grep", "find (a", {"pattern": "\\(a"}),
    ("grep", "grep for (", {"pattern": "\\("}),
    ("grep", "grep for )", {"pattern": "\\)"}),
    ("grep", "look for x)", {"pattern": "look\\ for\\ x\\)"}),
)

# Sentences and fragments that must never produce a broken argument.
JUNK = (
    "read", "find", "grep", "read the readme", "read .env",
    "read a.verylongextensionnamehere", "read model-v2.5.gguf",
    r"open C:\proj\main.py", "read C:/proj/main.py", r"read ..\..\secrets.txt",
    "grep for ..", "grep for (", "grep for )", "search for a{2,3}",
    "look for anything", "   ", "list_files", "show me **/*",
)

QUERY_CORPUS = tuple(q for _t, q, _w in FROZEN + FIXED) + JUNK


class TestFrozenBaseline(unittest.TestCase):
    """The pre-fix extractions that worked. Any change here is a regression."""

    def test_frozen_rows_unchanged(self):
        for tool, query, want in FROZEN:
            with self.subTest(tool=tool, query=query):
                self.assertEqual(_extract_simple_args(tool, query), want)


class TestFixedCases(unittest.TestCase):
    """The repairs plus their preserved neighbours."""

    def test_fixed_rows(self):
        for tool, query, want in FIXED:
            with self.subTest(tool=tool, query=query):
                self.assertEqual(_extract_simple_args(tool, query), want)

    def test_extension_chain_not_truncated(self):
        self.assertEqual(_extract_simple_args("read_file", "read app.tar.gz"),
                         {"path": "app.tar.gz"})

    def test_version_number_is_not_a_path(self):
        for query in ("read version 2.5 report", "read the 1.5 release notes"):
            with self.subTest(query=query):
                self.assertEqual(_extract_simple_args("read_file", query), {})

    def test_glob_keeps_trailing_star(self):
        self.assertEqual(_extract_simple_args("list_files", "show *foo* files"),
                         {"pattern": "*foo*"})
        self.assertEqual(_extract_simple_args("list_files", "show *foo files"),
                         {"pattern": "*foo"})

    def test_leading_dots_are_still_skipped(self):
        """Documents the deliberate body-class choice: adding "." would hand the
        sandbox paths like "../x" that core/agent_tools._ws_resolve rejects."""
        self.assertEqual(_extract_simple_args("read_file", "read ./src/app.py"),
                         {"path": "/src/app.py"})
        self.assertEqual(_extract_simple_args("read_file", "read ../x/y.md"),
                         {"path": "/x/y.md"})


class TestExtensionCap(unittest.TestCase):
    """Characterization, NOT a goal: a single extension segment is still capped at
    8 characters (pre-existing behaviour). If this fails because the cap was raised
    on purpose, update the row deliberately instead of deleting the test."""

    def test_long_extension_is_still_cut(self):
        self.assertEqual(_extract_simple_args("read_file", "read main.dockerfile"),
                         {"path": "main.dockerfi"})


class TestSafeGrepPattern(unittest.TestCase):

    def test_valid_regex_passes_through(self):
        for pat in ("router_route", "a+b*c", "[a-z]+", r"\bdef\b",
                    "look at data (draft)"):
            with self.subTest(pat=pat):
                self.assertEqual(_safe_grep_pattern(pat), pat)

    def test_invalid_regex_is_escaped(self):
        for pat in ("[unclosed", "(a", "(", ")", "look for x)", "a{2,3"):
            with self.subTest(pat=pat):
                escaped = _safe_grep_pattern(pat)
                re.compile(escaped)                      # must always compile
                self.assertTrue(re.search(escaped, pat))  # and still match itself


class TestInvariants(unittest.TestCase):
    """What the router lane may never emit, whatever the sentence looks like."""

    def test_every_grep_pattern_compiles(self):
        """An uncompilable pattern made tool_grep raise before doing any work."""
        for query in QUERY_CORPUS:
            with self.subTest(query=query):
                pat = _extract_simple_args("grep", query).get("pattern")
                if pat is not None:
                    re.compile(pat)

    def test_paths_are_never_absolute(self):
        for query in QUERY_CORPUS:
            path = _extract_simple_args("read_file", query).get("path")
            if path is None:
                continue
            with self.subTest(query=query):
                self.assertNotIn(":", path, "drive letter / scheme leaked into a path")
                self.assertNotIn("..", path.split("/"))
                self.assertNotIn("..", path.split("\\"))

    def test_paths_stay_inside_the_workspace(self):
        """The normalization _ws_resolve applies, then a real containment check: a
        router-extracted path must never escape the workspace root."""
        ws = Path(tempfile.mkdtemp()).resolve()
        for query in QUERY_CORPUS:
            path = _extract_simple_args("read_file", query).get("path")
            if path is None:
                continue
            clean = path.strip().replace("\\", "/").lstrip("/")
            if not clean or clean == ".":
                continue
            with self.subTest(query=query):
                target = (ws / clean).resolve()
                self.assertTrue(target == ws or ws in target.parents,
                                f"{path!r} escapes the workspace")

    def test_return_shape_is_unchanged(self):
        """Callers unpack the dict as `args` (core/small_model.py::laya_route)."""
        allowed = {(), ("pattern",), ("path",)}
        for tool in ("list_files", "read_file", "grep", "write_file", "run_python"):
            for query in ("read a.py", "list *.py", "grep x", "grep for [bad"):
                with self.subTest(tool=tool, query=query):
                    got = _extract_simple_args(tool, query)
                    self.assertIsInstance(got, dict)
                    self.assertIn(tuple(sorted(got)), allowed)


class TestContract(unittest.TestCase):
    """Structural guarantees kept by the refactor."""

    @staticmethod
    def _callers(name):
        tree = ast.parse(SRC.read_text(encoding="utf-8"))
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for inner in ast.walk(node):
                    if (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
                            and inner.func.id == name):
                        found.add(node.name)
        return found

    def test_extractor_is_laya_only(self):
        """Needle generates its own arguments (generative), so it must never depend
        on the regex extractor -- only the Laya decision path may."""
        self.assertEqual(self._callers("_extract_simple_args"), {"laya_route"})

    def test_helper_is_private_to_the_extractor(self):
        self.assertEqual(self._callers("_safe_grep_pattern"), {"_extract_simple_args"})

    def test_router_surface_unchanged(self):
        from core import small_model
        for name in ("router_route", "router_available", "router_engine_name",
                     "needle_route", "laya_route", "reset_router_failures",
                     "_extract_simple_args", "_safe_grep_pattern"):
            with self.subTest(name=name):
                self.assertTrue(hasattr(small_model, name), f"{name} disappeared")


if __name__ == "__main__":
    unittest.main()