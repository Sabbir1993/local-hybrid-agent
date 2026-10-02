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


class CallHierarchyTests(unittest.TestCase):
    def test_caller_symbol_identified_in_calls(self):
        from core.code_intel import ast_index as idx
        calls = idx.find_symbol_callers("get_user", REPO)
        callers = sorted({c.get("caller") for c in calls if c.get("caller")})
        self.assertIn("admin_lookup", callers)
        self.assertIn("user_profile", callers)
        self.assertIn("user_settings", callers)

    def test_find_symbol_callees(self):
        from core.code_intel import ast_index as idx
        callees = idx.find_symbol_callees("user_profile", REPO)
        names = [c["name"] for c in callees]
        self.assertIn("get_user", names)
        # target definition resolved across repo
        get_user_callee = [c for c in callees if c["name"] == "get_user"][0]
        def_files = [d["file"] for d in get_user_callee["definitions"]]
        self.assertIn("db.py", def_files)

    def test_get_call_hierarchy_graph(self):
        from core.code_intel import ast_index as idx
        graph = idx.get_call_hierarchy("get_user", REPO)
        self.assertEqual(graph["symbol"], "get_user")
        incoming_callers = {c.get("caller") for c in graph["incoming_callers"]}
        self.assertTrue({"admin_lookup", "user_profile", "user_settings"}.issubset(incoming_callers))


if __name__ == "__main__":
    unittest.main()

