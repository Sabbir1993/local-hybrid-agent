"""tests/test_early_exit_report.py - the early-exit diagnostic, pinned on synthetic DBs.

RED: the 27% early-exit population (runs that die at step 1-2) had no
diagnostic at all, and the one number people reached for (a 44% edit_file
failure rate) came from a query that counted tool_start rows as failures. This
pins the corrected pairing - a call with both a start and a completion row is
ONE call - plus the cause-code attribution that makes the population splittable.

Every fixture here is synthetic, so the expected numbers are exact.
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.early_exit_report import analyse, wilson


def make_db(rows_events=(), rows_runs=()):
    """Build a usage.db-shaped file in a temp dir and return its path."""
    tmp = tempfile.TemporaryDirectory(prefix="early_exit_")
    path = Path(tmp.name) / "usage.db"
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE route_events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
                  run_id TEXT NOT NULL, step INTEGER, category TEXT, lane TEXT, reason TEXT,
                  router_tool TEXT, router_conf REAL, escalated INTEGER DEFAULT 0, escalate_reason TEXT,
                  tool_name TEXT, tool_ok INTEGER, duration_s REAL, outcome TEXT, model TEXT, clf TEXT,
                  tool_err TEXT)""")
    db.execute("""CREATE TABLE route_runs (run_id TEXT PRIMARY KEY, ts REAL NOT NULL, user_id INTEGER,
                  mode TEXT, category TEXT, steps INTEGER DEFAULT 0, outcome TEXT, rating INTEGER,
                  detail TEXT)""")
    db.executemany("""INSERT INTO route_events (ts, run_id, step, category, lane, reason, tool_name,
                      tool_ok) VALUES (?,?,?,?,?,?,?,?)""", rows_events)
    db.executemany("""INSERT INTO route_runs (run_id, ts, mode, steps, outcome, detail)
                      VALUES (?,?,?,?,?,?)""", rows_runs)
    db.commit()
    db.close()
    return tmp, path


class WilsonTests(unittest.TestCase):
    def test_known_bands(self):
        self.assertEqual(wilson(0, 0), (0.0, 0.0))
        lo, hi = wilson(6, 95)
        self.assertLess(lo, 0.063)
        self.assertGreater(hi, 0.063)
        self.assertLessEqual(lo, 0.063)
        self.assertGreaterEqual(hi, 0.063)
        lo0, hi0 = wilson(0, 20)
        self.assertEqual(lo0, 0.0)
        self.assertGreater(hi0, 0.0)

    def test_band_narrows_with_n(self):
        narrow = wilson(60, 1200)
        wide = wilson(6, 60)
        self.assertLess(narrow[1] - narrow[0], wide[1] - wide[0])


class PairingTests(unittest.TestCase):
    """The bug that produced the phantom 44% must stay fixed here."""

    def test_a_call_with_start_and_completion_is_one_call(self):
        tmp, path = make_db(
            rows_events=[(1.0, "r1", 0, "action", "main", "tool_start", "edit_file", None),
                    (2.0, "r1", 0, "action", "main", "tool", "edit_file", 1)],
            rows_runs=[("r1", 1.0, "all-local", 1, "answered", None)])
        try:
            out = analyse(path)
        finally:
            tmp.cleanup()
        tools = out["tools"]["edit_file"]
        self.assertEqual(tools["calls"], 1, "start + completion counted as one call")
        self.assertEqual(tools["fail_pct"], 0.0)

    def test_tool_failure_rate_excludes_start_rows(self):
        tmp, path = make_db(
            rows_events=[(1.0, "r1", 0, "action", "main", "tool_start", "edit_file", None),
                    (2.0, "r1", 0, "action", "main", "tool", "edit_file", 0),
                    (3.0, "r2", 0, "action", "main", "tool_start", "edit_file", None),
                    (4.0, "r2", 0, "action", "main", "tool", "edit_file", 1)],
            rows_runs=[("r1", 1.0, "all-local", 1, "error", "err:ValueError"),
                  ("r2", 1.0, "all-local", 1, "answered", None)])
        try:
            out = analyse(path)
        finally:
            tmp.cleanup()
        tools = out["tools"]["edit_file"]
        self.assertEqual(tools["calls"], 2)
        self.assertEqual(tools["failed"], 1)
        self.assertAlmostEqual(tools["fail_pct"], 50.0, places=1)


class EarlyExitTests(unittest.TestCase):
    def test_populations_are_split_by_cause_code(self):
        tmp, path = make_db(
            rows_events=[(1.0, "r1", 0, "action", "main", "tool_start", None, None),
                    (2.0, "r2", 0, "action", "router", "tool_start", None, None),
                    (3.0, "r3", 0, "action", "main", "tool_start", None, None),
                    (4.0, "r4", 0, "action", "main", "tool_start", None, None)],
            rows_runs=[("r1", 1.0, "main-cloud-rest-local", 1, "error", "err:ValueError"),
                  ("r2", 1.0, "main-cloud-rest-local", 2, "synthesized", "synth:fallback_confirmation"),
                  ("r3", 1.0, "all-local", 40, "answered", None),
                  ("r4", 1.0, "all-local", 60, "max_steps", None)])
        try:
            out = analyse(path)
        finally:
            tmp.cleanup()
        self.assertEqual(out["runs"], 4)
        self.assertEqual(out["outcomes"]["error"], 1)
        self.assertEqual(out["outcomes"]["answered"], 1)
        self.assertEqual(out["early"]["n"], 2)
        self.assertEqual(out["early"]["causes"]["err:ValueError"], 1)
        self.assertEqual(out["early"]["causes"]["synth:fallback_confirmation"], 1)
        self.assertEqual(out["ceiling"]["n"], 1)

    def test_early_exit_is_defined_by_step_count_not_by_outcome_alone(self):
        # a max_steps run is never "early" however few steps it recorded
        tmp, path = make_db(
            rows_events=[], rows_runs=[("r1", 1.0, "m", 2, "max_steps", None)])
        try:
            out = analyse(path)
        finally:
            tmp.cleanup()
        self.assertEqual(out["early"]["n"], 0)
        self.assertEqual(out["ceiling"]["n"], 1)

    def test_unattributed_runs_are_counted_not_hidden(self):
        # pre-change rows carry no detail: the report must say so rather than
        # implying the population is understood
        tmp, path = make_db([], [("r1", 1.0, "m", 1, "synthesized", None)])
        try:
            out = analyse(path)
        finally:
            tmp.cleanup()
        self.assertEqual(out["early"]["n"], 1)
        self.assertEqual(out["early"]["unattributed"], 1)
        self.assertEqual(out["attribution_ready"], False)

    def test_attribution_ready_when_codes_exist(self):
        tmp, path = make_db([], [("r1", 1.0, "m", 1, "error", "err:TypeError")])
        try:
            out = analyse(path)
        finally:
            tmp.cleanup()
        self.assertTrue(out["attribution_ready"])


class Step0RouterTests(unittest.TestCase):
    def test_router_pass_rate_is_reported_per_population(self):
        tmp, path = make_db(
            rows_events=[(1.0, "r1", 0, "action", "router", "router_pass", None, None),
                    (2.0, "r2", 0, "action", "router", "router_pass", None, None),
                    (3.0, "r3", 0, "action", "router", "tool", None, None)],
            rows_runs=[("r1", 1.0, "m", 1, "error", "err:X"),
                  ("r2", 1.0, "m", 1, "error", "err:Y"),
                  ("r3", 1.0, "m", 5, "answered", None)])
        try:
            out = analyse(path)
        finally:
            tmp.cleanup()
        self.assertEqual(out["step0_router"]["early"], {"router_pass": 2})
        self.assertEqual(out["step0_router"]["answered"], {"tool": 1})
        self.assertIn("hypothesis", out["notes"][0].lower())


if __name__ == "__main__":
    unittest.main()