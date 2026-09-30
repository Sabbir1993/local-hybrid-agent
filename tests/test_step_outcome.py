import os
import sqlite3
import tempfile
import unittest

from core import step_outcome as so


class ClassifyStepTests(unittest.TestCase):
    def test_codes(self):
        self.assertEqual(so.classify_step("", [{"x": 1}]), so.TOOL_CALL)
        self.assertEqual(so.classify_step("", [{"x": 1}], parse_failed=True), so.PARSE_FAIL)
        self.assertEqual(so.classify_step("", []), so.EMPTY)
        self.assertEqual(so.classify_step("   \n", None), so.EMPTY)
        self.assertEqual(so.classify_step("[Let me start...]", []), so.NO_TOOL_CALL)
        self.assertEqual(so.classify_step("anything", [{"x": 1}], is_loop=True), so.LOOP)

    def test_failure_depends_on_request_kind(self):
        # plain text is a fine answer to a question, a failure for an action request
        self.assertFalse(so.failed(so.NO_TOOL_CALL, action_expected=False))
        self.assertTrue(so.failed(so.NO_TOOL_CALL, action_expected=True))
        self.assertTrue(so.failed(so.EMPTY, action_expected=True))
        self.assertFalse(so.failed(so.TOOL_CALL, action_expected=True))
        # transport / parse / loop failures fail either way
        for o in (so.TRANSPORT_ERROR, so.PARSE_FAIL, so.LOOP):
            self.assertTrue(so.failed(o, action_expected=False))


class RouteLogMigrationTests(unittest.TestCase):
    def test_old_table_gains_columns_and_records_outcome(self):
        from core import route_log
        old = sqlite3.connect(":memory:")
        old.execute("""CREATE TABLE route_events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
            run_id TEXT NOT NULL, step INTEGER, category TEXT, lane TEXT, reason TEXT, router_tool TEXT,
            router_conf REAL, escalated INTEGER DEFAULT 0, escalate_reason TEXT, tool_name TEXT,
            tool_ok INTEGER, duration_s REAL)""")
        orig = route_log._usage_db
        route_log._usage_db = old
        try:
            route_log._init()
            cols = {r[1] for r in old.execute("PRAGMA table_info(route_events)")}
            self.assertLessEqual({"outcome", "model"}, cols)
            route_log._init()   # idempotent
            route_log.event("r1", 0, "action", "executor", "executor_default",
                            outcome=so.NO_TOOL_CALL, model="m")
            row = old.execute("SELECT outcome, model FROM route_events").fetchone()
            self.assertEqual(row, (so.NO_TOOL_CALL, "m"))
        finally:
            route_log._usage_db = orig


if __name__ == "__main__":
    unittest.main()
