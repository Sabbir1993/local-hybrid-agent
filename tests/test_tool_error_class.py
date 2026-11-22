"""tests/test_tool_error_class.py - failure attribution for tool telemetry.

RED for E-edit measurement: route_events recorded tool_ok with NO reason, so the
6.3% edit_file failure rate could not be split into "model sent a block that
does not match" vs "ambiguous" vs "verification failed". Every attempt to
attribute it from usage.db came back blank. This pins a fixed code vocabulary
(classify_tool_result) and the write path that stores it.

Codes only, never text: an error string can carry file paths and content
snippets, and usage.db is retained data (core/route_log.py purge keeps 30 days
of real user workspaces).
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from core import route_log


class ToolErrorClassTests(unittest.TestCase):
    def test_ok_results_have_no_class(self):
        self.assertIsNone(route_log.classify_tool_result("edited a.py at line 3: 1 replacement(s)"))
        self.assertIsNone(route_log.classify_tool_result("verify: OK"))

    def test_edit_match_failures_are_distinguished(self):
        cases = {
            "error: old_string not found in the file. Closest lines: line 4: 'x = 1'": "no_match",
            "error: old_string appears 2 times (lines 1, 3) - include more surrounding lines": "ambiguous",
            "error: old_string matches 2 places once whitespace is ignored (lines 1, 3)": "ambiguous",
            "error: old_string is close to the text at 2 places (lines 1, 3)": "ambiguous",
            "error: old_string is empty": "invalid_args",
            "error: old_string and new_string are identical - nothing to change": "invalid_args",
        }
        for text, want in cases.items():
            self.assertEqual(route_log.classify_tool_result(text), want, text[:60])

    def test_diff_failures_are_distinguished(self):
        cases = {
            "error: hunk 2 drifted: its context lines no longer match the file": "diff_drift",
            "error: no hunks found in diff - send a unified diff": "diff_malformed",
            "error: diff is empty": "diff_malformed",
            "error: hunks overlap - the diff modifies the same lines twice": "diff_malformed",
            "error: pass either diff or old_string/new_string, not both": "invalid_args",
        }
        for text, want in cases.items():
            self.assertEqual(route_log.classify_tool_result(text), want, text[:60])

    def test_permission_and_state_failures_are_distinguished(self):
        cases = {
            "error: sandbox violation: path cannot traverse outside workspace root (..)": "sandbox",
            "error: read old.py before editing it - call read_file first": "not_read",
            "error: notes.md does not exist yet. Create it with write_file": "not_found",
            "error: old.py already exists (10 lines). Change part of it with edit_file": "exists",
            "File not found: gone.py": "not_found",
        }
        for text, want in cases.items():
            self.assertEqual(route_log.classify_tool_result(text), want, text[:60])

    def test_write_and_plan_failures_are_attributable(self):
        """Production 2026-11 collapsed these into 'error', hiding the failing component."""
        cases = {
            "error: pre-save syntax validation failed for x.py: invalid syntax (file was not written)": "syntax",
            "error: this write_file content is about 11622 tokens; one call may carry at most 8192.": "too_big",
            "error: a.txt would grow past 2048 KB - split it into several files.": "too_big",
            "error: overwrite=true would replace 80 lines of x.py with only 12 - that deletes sections": "shrink",
            "error: content required for append_file": "invalid_args",
            "error: database is locked": "locked",
            "error: timed out after 30s (config agent.exec_timeout_s)": "timeout",
            "error: RuntimeError: companion is not connected": "connectivity",
            "error: ConnectError: all connection attempts failed": "connectivity",
            "error: ValueError: no active session for plan tracking (session_id missing)": "no_session",
            "error: ValueError: the plan for this task is already finished (every step is done or failed).": "plan_exists",
            "error: ValueError: this exact plan already exists - do not call create_plan again": "plan_exists",
        }
        for text, want in cases.items():
            self.assertEqual(route_log.classify_tool_result(text), want, text[:70])

    def test_raised_exceptions_keep_only_their_type(self):
        self.assertEqual(route_log.classify_tool_result("error: RuntimeError: /secret/path/x.py boom"), "exc:RuntimeError")
        self.assertEqual(route_log.classify_tool_result("error: ValueError: bad /home/alice/token"), "exc:ValueError")
        self.assertEqual(route_log.classify_tool_result("error: TimeoutError: slow"), "exc:TimeoutError")

    def test_unknown_failure_falls_back_to_error(self):
        self.assertEqual(route_log.classify_tool_result("error: something new happened"), "error")
        self.assertEqual(route_log.classify_tool_result(""), "error")

    def test_no_free_text_is_ever_returned(self):
        secret = "error: read /home/alice/secret-project/token.py before editing it"
        code = route_log.classify_tool_result(secret)
        self.assertEqual(code, "not_read")
        self.assertNotIn("alice", code)
        self.assertNotIn("secret", code)


class ToolErrorWriteTests(unittest.TestCase):
    def test_event_persists_tool_err_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "usage.db"
            orig = route_log._usage_db
            route_log._usage_db = sqlite3.connect(db_path)
            route_log._init()
            try:
                route_log.event("run-1", 3, "action", "main", "tool",
                                tool_name="edit_file", tool_ok=False,
                                tool_err="no_match")
                row = route_log._usage_db.execute(
                    "SELECT tool_err FROM route_events WHERE run_id='run-1'").fetchone()
            finally:
                route_log._usage_db.close()
                route_log._usage_db = orig
        self.assertEqual(row[0], "no_match")


if __name__ == "__main__":
    unittest.main()