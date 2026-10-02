"""tests/test_code_intel.py - R9 RED: tree-sitter symbol search.

grep answers "where is this string"; the agent needs "where is this SYMBOL
defined, what calls it, what does this file contain" - cross-file, with line
numbers, without reading 10 files into context. Staged fixture repo under
tests/fixtures/code_repo: same-name symbols in two modules, a rename pair,
three call sites across two files, a JS helper.

Run: python -m unittest tests.test_code_intel -v
"""

import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent / "fixtures" / "code_repo"


class SymbolDefinitionTests(unittest.TestCase):
    def test_definition_with_signature_and_docstring(self):
        from core.code_intel import ast_index as idx
        hits = idx.find_symbol_definition("authenticate", REPO,
                                          path_hint="auth.py")
        self.assertEqual(len(hits), 1)
        h = hits[0]
        self.assertEqual(h["file"], "auth.py")
        self.assertEqual(h["kind"], "function")
        self.assertIn("password", h["signature"])
        self.assertIn("directory", h["docstring"])

    def test_same_name_two_modules_disambiguated(self):
        from core.code_intel import ast_index as idx
        all_hits = idx.find_symbol_definition("authenticate", REPO)
        files = sorted(h["file"] for h in all_hits)
        self.assertEqual(files, ["auth.py", "legacy_auth.py"])
        legacy = [h for h in all_hits if h["file"] == "legacy_auth.py"][0]
        self.assertNotIn("password", legacy["signature"])

    def test_method_definition_inside_class(self):
        from core.code_intel import ast_index as idx
        hits = idx.find_symbol_definition("login", REPO)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["class"], "AuthManager")
        self.assertEqual(hits[0]["file"], "auth.py")

    def test_javascript_definition(self):
        from core.code_intel import ast_index as idx
        hits = idx.find_symbol_definition("lookupRecord", REPO)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["file"], "helpers.js")
        self.assertIn("table", hits[0]["signature"])


class CallerTests(unittest.TestCase):
    def test_three_call_sites_across_two_files(self):
        from core.code_intel import ast_index as idx
        calls = idx.find_symbol_callers("get_user", REPO)
        by_file = {}
        for c in calls:
            by_file.setdefault(c["file"], []).append(c["line"])
        self.assertEqual(sorted(by_file), ["admin.py", "routes.py"])
        self.assertEqual(len(by_file["routes.py"]), 2)
        self.assertEqual(len(by_file["admin.py"]), 1)

    def test_rename_old_name_delegates_to_new(self):
        from core.code_intel import ast_index as idx
        calls = idx.find_symbol_callers("fetch_record", REPO)
        files = sorted({c["file"] for c in calls})
        self.assertIn("db.py", files)      # retrieve_record delegates
        self.assertIn("routes.py", files)  # record_view calls it


class OutlineTests(unittest.TestCase):
    def test_outline_skeleton_without_bodies(self):
        from core.code_intel import ast_index as idx
        out = idx.get_file_outline(REPO / "auth.py")
        kinds = [(e["kind"], e["name"]) for e in out]
        self.assertIn(("function", "authenticate"), kinds)
        self.assertIn(("class", "AuthManager"), kinds)
        self.assertIn(("method", "login"), kinds)
        self.assertIn(("method", "logout"), kinds)
        blob = "\n".join(e.get("signature", "") for e in out)
        self.assertNotIn("start_session", blob)  # bodies stay out of context


if __name__ == "__main__":
    unittest.main()
