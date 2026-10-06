"""tests/test_patch_verifier.py — Phase 3: Raw Intelligence & Code Solving.

Tests for:
  1. core/agent_loop/patch_verifier.py:
       - localize_fault(): traceback parsing, error class extraction, fingerprinting
       - validate_file_syntax(): Python compile() check, JS heuristic, unsupported language
       - check_import_guard(): import resolution
       - VerificationReceipt: record/replay cycle, same_error_count, receipt_for()
       - check_verification_loop(): OK / RETRY / ABANDON paths
       - syntax_check_actions(): integration with action list format
  2. core/agent_loop/plan_guard.py:
       - check_syntax_before_done(): integration gate rejects broken Python writes
       - check_update(): now gates on syntax errors too

All tests: pure python, no subprocess, no server, no disk I/O except
validate_file_syntax with a source= kwarg (no disk read).
Run: python -m unittest tests.test_patch_verifier -v
"""

import unittest

from core.agent_loop.patch_verifier import (
    ABANDON,
    OK,
    RETRY,
    FaultSignal,
    SyntaxCheckResult,
    VerificationReceipt,
    _check_js_syntax_heuristic,
    _check_python_syntax,
    check_import_guard,
    check_verification_loop,
    localize_fault,
    syntax_check_actions,
    validate_file_syntax,
)
from core.agent_loop.plan_guard import (
    check_syntax_before_done,
    check_update,
)


# ---------------------------------------------------------------------------
# 1. localize_fault — traceback parsing
# ---------------------------------------------------------------------------
TRACEBACK_SAMPLE = """\
Traceback (most recent call last):
  File "/workspace/core/memory/search.py", line 47, in search_memory_hybrid
    results = embed_fn(queries)
  File "/workspace/tests/test_memory.py", line 112, in test_expand_query
    self.assertEqual(len(variants), 3)
AssertionError: 3 != 1
"""

IMPORT_ERROR_SAMPLE = """\
Traceback (most recent call last):
  File "/workspace/routes/agent/run.py", line 8, in <module>
    from core.missing_module import something
ModuleNotFoundError: No module named 'core.missing_module'
"""

TYPE_ERROR_SAMPLE = """\
Traceback (most recent call last):
  File "/workspace/core/router_policy.py", line 23, in contextual_query
    result = model.predict(query, extra=True)
TypeError: predict() got an unexpected keyword argument 'extra'
"""


class LocalizeFaultTests(unittest.TestCase):

    def test_parses_assertion_error(self):
        sig = localize_fault(TRACEBACK_SAMPLE)
        self.assertEqual(sig.error_class, "AssertionError")
        self.assertGreater(len(sig.traceback_sites), 0)

    def test_last_site_is_test_line(self):
        sig = localize_fault(TRACEBACK_SAMPLE)
        last = sig.traceback_sites[-1]
        self.assertIn("test_memory.py", last["file"])
        self.assertEqual(last["line"], 112)

    def test_primary_file_skips_test_framework(self):
        """primary_file should prefer non-test-framework files."""
        sig = localize_fault(TRACEBACK_SAMPLE)
        # Both sites are non-framework; it should pick the inner-most non-test
        # file (search.py or test_memory.py but NOT _pytest/)
        pf = sig.primary_file
        self.assertIsNotNone(pf)
        self.assertNotIn("_pytest", pf or "")
        self.assertNotIn("unittest", pf or "")

    def test_import_error_category(self):
        sig = localize_fault(IMPORT_ERROR_SAMPLE)
        self.assertEqual(sig.error_class, "ModuleNotFoundError")
        self.assertEqual(sig.category, "import")
        self.assertIn("module", sig.category_note)

    def test_type_error_category(self):
        sig = localize_fault(TYPE_ERROR_SAMPLE)
        self.assertEqual(sig.error_class, "TypeError")
        self.assertEqual(sig.category, "signature")

    def test_fingerprint_is_stable(self):
        sig1 = localize_fault(TRACEBACK_SAMPLE)
        sig2 = localize_fault(TRACEBACK_SAMPLE)
        self.assertEqual(sig1.fingerprint, sig2.fingerprint)
        self.assertGreater(len(sig1.fingerprint), 0)

    def test_different_errors_different_fingerprints(self):
        sig_a = localize_fault(TRACEBACK_SAMPLE)
        sig_b = localize_fault(IMPORT_ERROR_SAMPLE)
        self.assertNotEqual(sig_a.fingerprint, sig_b.fingerprint)

    def test_empty_output_returns_empty_signal(self):
        sig = localize_fault("")
        self.assertTrue(sig.is_empty())

    def test_summary_non_empty_for_real_error(self):
        sig = localize_fault(TRACEBACK_SAMPLE)
        self.assertGreater(len(sig.summary), 5)

    def test_all_sites_extracted(self):
        sig = localize_fault(TRACEBACK_SAMPLE)
        self.assertEqual(len(sig.traceback_sites), 2)


# ---------------------------------------------------------------------------
# 2. validate_file_syntax — Python
# ---------------------------------------------------------------------------
class PythonSyntaxTests(unittest.TestCase):

    def test_clean_python_passes(self):
        source = "def foo(x):\n    return x + 1\n"
        r = _check_python_syntax(source, "clean.py")
        self.assertTrue(r.ok)

    def test_syntax_error_detected(self):
        source = "def foo(x\n    return x\n"   # missing closing paren
        r = _check_python_syntax(source, "bad.py")
        self.assertFalse(r.ok)
        self.assertGreater(len(r.error), 0)

    def test_indentation_error_detected(self):
        source = "def foo():\nreturn 1\n"   # bad indentation
        r = _check_python_syntax(source, "indent.py")
        self.assertFalse(r.ok)

    def test_line_number_reported(self):
        source = "x = 1\ny = (\n"   # unclosed paren
        r = _check_python_syntax(source, "unclosed.py")
        self.assertFalse(r.ok)
        self.assertIsNotNone(r.line)

    def test_complex_valid_python(self):
        source = (
            "from typing import List, Optional\n"
            "import re\n\n"
            "class Foo:\n"
            "    def bar(self, x: int) -> Optional[str]:\n"
            "        return str(x) if x > 0 else None\n"
        )
        r = _check_python_syntax(source, "complex.py")
        self.assertTrue(r.ok)

    def test_validate_file_syntax_routes_python(self):
        source = "x = 1 +\n"  # syntax error
        r = validate_file_syntax("foo.py", source=source)
        self.assertFalse(r.ok)
        self.assertIn("foo.py", r.file)

    def test_validate_file_syntax_clean(self):
        source = "x = 1 + 2\n"
        r = validate_file_syntax("foo.py", source=source)
        self.assertTrue(r.ok)

    def test_as_receipt_error_contains_file(self):
        r = SyntaxCheckResult(ok=False, error="unexpected EOF", line=10,
                              col=5, file="my_module.py")
        msg = r.as_receipt_error()
        self.assertIn("my_module.py", msg)
        self.assertIn("line 10", msg)
        self.assertIn("unexpected EOF", msg)


# ---------------------------------------------------------------------------
# 3. validate_file_syntax — JS heuristic
# ---------------------------------------------------------------------------
class JSSyntaxTests(unittest.TestCase):

    def test_clean_js_passes(self):
        source = "function foo(x) {\n  return x + 1;\n}\n"
        r = _check_js_syntax_heuristic(source, "foo.js")
        self.assertTrue(r.ok)

    def test_unmatched_close_brace_caught(self):
        source = "function foo() {\n  return 1;\n}\n}"   # extra }
        r = _check_js_syntax_heuristic(source, "bad.js")
        self.assertFalse(r.ok)
        self.assertIn("}", r.error)

    def test_validate_file_syntax_routes_js(self):
        r = validate_file_syntax("app.js", source="{"*10)   # 10 unclosed braces > threshold=5
        self.assertFalse(r.ok)

    def test_unsupported_ext_always_passes(self):
        r = validate_file_syntax("styles.css", source="body { color: red; }")
        self.assertTrue(r.ok)

    def test_comment_lines_skipped(self):
        source = "// this has unmatched { but it's a comment\nconst x = 1;\n"
        r = _check_js_syntax_heuristic(source, "commented.js")
        self.assertTrue(r.ok)


# ---------------------------------------------------------------------------
# 4. check_import_guard
# ---------------------------------------------------------------------------
class ImportGuardTests(unittest.TestCase):

    def test_stdlib_import_passes(self):
        source = "import os\nimport re\nfrom pathlib import Path\n"
        r = check_import_guard(source, "uses_stdlib.py")
        self.assertTrue(r.ok)

    def test_nonexistent_toplevel_import_fails(self):
        source = "import xyzzy_nonexistent_pkg_abc\n"
        r = check_import_guard(source, "bad_import.py")
        self.assertFalse(r.ok)
        self.assertIn("xyzzy_nonexistent_pkg_abc", r.error)

    def test_relative_import_skipped(self):
        """Relative imports (from . import X) must not trigger the guard."""
        source = "from . import search\nfrom ..config import DEFAULTS\n"
        r = check_import_guard(source, "relative.py")
        self.assertTrue(r.ok)

    def test_non_python_file_skipped(self):
        source = "import xyzzy_nonexistent\n"  # same bad import
        r = check_import_guard(source, "file.js")
        self.assertTrue(r.ok)   # JS files not checked

    def test_syntax_error_in_source_skipped_gracefully(self):
        """If source itself has a syntax error, import guard should not crash."""
        source = "def broken(\n"
        r = check_import_guard(source, "broken.py")
        # Should not raise; returns ok=True (syntax check already caught this)
        self.assertTrue(r.ok)


# ---------------------------------------------------------------------------
# 5. VerificationReceipt — record/replay
# ---------------------------------------------------------------------------
class VerificationReceiptTests(unittest.TestCase):

    def test_initial_state_not_passed(self):
        vr = VerificationReceipt()
        self.assertFalse(vr.passed)
        self.assertEqual(vr.attempt_count, 0)
        self.assertEqual(vr.same_error_count, 0)

    def test_record_edit_deduplicates(self):
        vr = VerificationReceipt()
        vr.record_edit("foo.py")
        vr.record_edit("foo.py")
        vr.record_edit("bar.py")
        self.assertEqual(len(vr.edited_files), 2)

    def test_record_test_deduplicates(self):
        vr = VerificationReceipt()
        vr.record_test("python -m unittest tests.test_foo")
        vr.record_test("python -m unittest tests.test_foo")
        self.assertEqual(len(vr.test_commands), 1)

    def test_record_pass(self):
        vr = VerificationReceipt()
        vr.record_result(passed=True)
        self.assertTrue(vr.passed)
        self.assertEqual(vr.attempt_count, 1)
        self.assertIsNone(vr.fault)

    def test_record_failure_localizes_fault(self):
        vr = VerificationReceipt()
        vr.record_result(passed=False, output=TRACEBACK_SAMPLE)
        self.assertFalse(vr.passed)
        self.assertIsNotNone(vr.fault)
        self.assertEqual(vr.fault.error_class, "AssertionError")

    def test_same_error_count_increments(self):
        vr = VerificationReceipt()
        for _ in range(3):
            vr.record_result(passed=False, output=TRACEBACK_SAMPLE)
        self.assertEqual(vr.same_error_count, 3)

    def test_different_error_resets_streak(self):
        vr = VerificationReceipt()
        vr.record_result(passed=False, output=TRACEBACK_SAMPLE)
        vr.record_result(passed=False, output=IMPORT_ERROR_SAMPLE)
        self.assertEqual(vr.same_error_count, 1)

    def test_receipt_for_passed(self):
        vr = VerificationReceipt(edited_files=["core/foo.py"],
                                  test_commands=["tests.test_foo"],
                                  passed=True, attempt_count=1)
        msg = vr.receipt_for(3)
        self.assertIn("#3", msg)
        self.assertIn("VERIFIED", msg)
        self.assertIn("core/foo.py", msg)

    def test_receipt_for_failed(self):
        vr = VerificationReceipt()
        vr.record_result(passed=False, output=TRACEBACK_SAMPLE)
        msg = vr.receipt_for(5)
        self.assertIn("#5", msg)
        self.assertIn("UNVERIFIED", msg)
        self.assertIn("AssertionError", msg)


# ---------------------------------------------------------------------------
# 6. check_verification_loop
# ---------------------------------------------------------------------------
class VerificationLoopTests(unittest.TestCase):

    def test_passed_returns_ok(self):
        vr = VerificationReceipt()
        vr.record_result(passed=True)
        status, msg = check_verification_loop(vr)
        self.assertEqual(status, OK)
        self.assertEqual(msg, "")

    def test_no_test_run_returns_retry(self):
        """If no test was recorded and require_test=True, should RETRY."""
        vr = VerificationReceipt()
        vr.record_edit("some_file.py")
        vr.record_result(passed=False, output="some error")
        vr2 = VerificationReceipt()  # fresh: no test_commands
        status, msg = check_verification_loop(vr2, require_test=True)
        self.assertEqual(status, RETRY)
        self.assertIn("test", msg.lower())

    def test_no_test_required_false_allows_pass(self):
        """With require_test=False, a passed result without test is OK."""
        vr = VerificationReceipt()
        vr.record_result(passed=True)
        status, _ = check_verification_loop(vr, require_test=False)
        self.assertEqual(status, OK)

    def test_single_failure_returns_retry(self):
        vr = VerificationReceipt()
        vr.record_test("python -m unittest tests.test_foo")
        vr.record_result(passed=False, output=TRACEBACK_SAMPLE)
        status, msg = check_verification_loop(vr)
        self.assertEqual(status, RETRY)
        self.assertIn("AssertionError", msg)

    def test_retry_message_contains_primary_file(self):
        vr = VerificationReceipt()
        vr.record_test("tests.test_foo")
        vr.record_result(passed=False, output=TRACEBACK_SAMPLE)
        _, msg = check_verification_loop(vr)
        # Should contain the primary file path from the traceback
        self.assertTrue(
            any(keyword in msg for keyword in ("search.py", "test_memory.py", "Check the traceback")),
            f"Expected file hint in retry message, got: {msg}"
        )

    def test_three_same_errors_returns_abandon(self):
        vr = VerificationReceipt()
        vr.record_test("tests.test_foo")
        for _ in range(3):
            vr.record_result(passed=False, output=TRACEBACK_SAMPLE)
        status, msg = check_verification_loop(vr)
        self.assertEqual(status, ABANDON)
        self.assertIn("3 times", msg)
        self.assertIn("root cause", msg.lower())

    def test_abandon_threshold_is_exactly_3(self):
        """Two identical errors → RETRY; three → ABANDON."""
        vr2 = VerificationReceipt()
        vr2.record_test("t")
        for _ in range(2):
            vr2.record_result(passed=False, output=TRACEBACK_SAMPLE)
        status2, _ = check_verification_loop(vr2)
        self.assertEqual(status2, RETRY)

    def test_generic_retry_when_no_traceback(self):
        """Generic RETRY message when there's no parseable traceback."""
        vr = VerificationReceipt()
        vr.record_test("tests.test_foo")
        vr.record_result(passed=False, output="FAILED (generic error, no traceback)")
        status, msg = check_verification_loop(vr)
        self.assertEqual(status, RETRY)
        self.assertIn("[verify]", msg)


# ---------------------------------------------------------------------------
# 7. syntax_check_actions — batch integration
# ---------------------------------------------------------------------------
class SyntaxCheckActionsTests(unittest.TestCase):

    def test_clean_write_no_failures(self):
        actions = [{"name": "write_file", "ok": True,
                    "args": {"path": "foo.py", "content": "x = 1\n"}}]
        self.assertEqual(syntax_check_actions(actions), [])

    def test_broken_python_write_flagged(self):
        actions = [{"name": "write_file", "ok": True,
                    "args": {"path": "foo.py", "content": "def bar(x\n    pass\n"}}]
        failures = syntax_check_actions(actions)
        self.assertEqual(len(failures), 1)
        self.assertFalse(failures[0].ok)
        self.assertIn("foo.py", failures[0].file)

    def test_failed_action_skipped(self):
        """Actions with ok=False must not be syntax-checked."""
        actions = [{"name": "write_file", "ok": False,
                    "args": {"path": "foo.py", "content": "def bar(x\n"}}]
        self.assertEqual(syntax_check_actions(actions), [])

    def test_non_write_action_skipped(self):
        actions = [{"name": "run_python", "ok": True,
                    "args": {"code": "def bar(x\n"}}]
        self.assertEqual(syntax_check_actions(actions), [])

    def test_multiple_writes_multiple_checks(self):
        actions = [
            {"name": "write_file", "ok": True,
             "args": {"path": "clean.py", "content": "x = 1\n"}},
            {"name": "write_file", "ok": True,
             "args": {"path": "broken.py", "content": "def foo(\n"}},
        ]
        failures = syntax_check_actions(actions)
        self.assertEqual(len(failures), 1)
        self.assertIn("broken.py", failures[0].file)

    def test_empty_actions_returns_empty(self):
        self.assertEqual(syntax_check_actions([]), [])


# ---------------------------------------------------------------------------
# 8. plan_guard integration — check_syntax_before_done, check_update
# ---------------------------------------------------------------------------
class PlanGuardSyntaxGateTests(unittest.TestCase):

    _ITEMS = [{"ord": 1, "text": "write a module", "status": "in_progress"}]

    def test_clean_python_action_not_blocked(self):
        actions = [{"name": "write_file", "ok": True,
                    "args": {"path": "module.py", "content": "def foo(): pass\n"}}]
        result = check_syntax_before_done(actions)
        self.assertIsNone(result)

    def test_broken_python_action_blocked(self):
        actions = [{"name": "write_file", "ok": True,
                    "args": {"path": "module.py", "content": "def foo(x\n    pass\n"}}]
        result = check_syntax_before_done(actions)
        self.assertIsNotNone(result)
        self.assertIn("syntax error", result)
        self.assertIn("module.py", result)

    def test_empty_actions_not_blocked(self):
        result = check_syntax_before_done([])
        self.assertIsNone(result)

    def test_check_update_done_with_broken_python_returns_error(self):
        """check_update('done') should now propagate the syntax error from the write."""
        items = [{"ord": 1, "text": "implement feature", "status": "in_progress"}]
        actions = [{"name": "write_file", "ok": True,
                    "args": {"path": "feature.py", "content": "class Foo(\n    pass\n"}}]
        result = check_update(items, item=1, status="done", actions=actions)
        self.assertIsNotNone(result)
        self.assertIn("syntax error", result)

    def test_check_update_done_clean_write_passes(self):
        items = [{"ord": 1, "text": "implement feature", "status": "in_progress"}]
        actions = [{"name": "write_file", "ok": True,
                    "args": {"path": "feature.py", "content": "class Foo: pass\n"}}]
        result = check_update(items, item=1, status="done", actions=actions)
        self.assertIsNone(result)

    def test_check_update_in_progress_not_syntax_checked(self):
        """Syntax gate must NOT run on in_progress transitions, only on done."""
        items = [{"ord": 1, "text": "implement", "status": "pending"}]
        actions = [{"name": "write_file", "ok": True,
                    "args": {"path": "x.py", "content": "def foo(\n"}}]
        result = check_update(items, item=1, status="in_progress", actions=actions)
        self.assertIsNone(result)   # in_progress gate only checks for concurrent steps


# ---------------------------------------------------------------------------
# 9. FaultSignal helpers
# ---------------------------------------------------------------------------
class FaultSignalTests(unittest.TestCase):

    def test_is_empty_on_default(self):
        self.assertTrue(FaultSignal().is_empty())

    def test_not_empty_with_sites(self):
        sig = FaultSignal(traceback_sites=[{"file": "f.py", "line": 1}])
        self.assertFalse(sig.is_empty())

    def test_primary_file_returns_none_on_empty(self):
        self.assertIsNone(FaultSignal().primary_file)

    def test_primary_line_returns_none_on_empty(self):
        self.assertIsNone(FaultSignal().primary_line)

    def test_primary_file_skips_pytest(self):
        sig = FaultSignal(traceback_sites=[
            {"file": "/workspace/core/router.py", "line": 10, "function": "route"},
            {"file": "/usr/lib/python3/dist-packages/_pytest/runner.py", "line": 100},
        ])
        pf = sig.primary_file
        self.assertIn("router.py", pf)


if __name__ == "__main__":
    unittest.main()
