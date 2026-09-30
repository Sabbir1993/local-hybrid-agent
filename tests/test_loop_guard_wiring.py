import re
import sqlite3
import unittest
from pathlib import Path

from core import route_log
from core.agent_loop import loop_guard as lg

RUN_PY = Path(__file__).resolve().parent.parent / "routes" / "agent" / "run.py"


class StopDetailIsStored(unittest.TestCase):
    def setUp(self):
        self._orig = route_log._usage_db
        route_log._usage_db = sqlite3.connect(":memory:")
        # an older database without the column
        route_log._usage_db.execute(
            "CREATE TABLE route_runs (run_id TEXT PRIMARY KEY, ts REAL NOT NULL, user_id INTEGER, mode TEXT, "
            "category TEXT, steps INTEGER DEFAULT 0, outcome TEXT, rating INTEGER)")
        route_log._init()

    def tearDown(self):
        route_log._usage_db = self._orig

    def test_column_is_added_and_written(self):
        route_log.run_start("r1", 1, "m", "action")
        route_log.run_end("r1", 12, "no_progress", detail="identical_result:run_python:3")
        row = route_log._usage_db.execute("SELECT steps, outcome, detail FROM route_runs").fetchone()
        self.assertEqual(row, (12, "no_progress", "identical_result:run_python:3"))

    def test_run_end_without_detail_still_works(self):
        route_log.run_start("r2", 1, "m", "action")
        route_log.run_end("r2", 3, "answered")
        self.assertIsNone(route_log._usage_db.execute("SELECT detail FROM route_runs").fetchone()[0])

    def test_no_progress_counts_as_a_bad_run_for_the_tuner(self):
        self.assertIn("no_progress", route_log.BAD_OUTCOMES)


class LoopIsWiredToTheGuard(unittest.TestCase):
    """Source-level checks that the pieces of the loop are connected (the loop itself is an async
    generator too entangled with servers to drive in a unit test)."""
    src = RUN_PY.read_text(encoding="utf-8")

    def test_every_executed_call_is_recorded_and_the_verdict_acted_on(self):
        self.assertIn("guard.record(", self.src)
        for level in ("_lg.NUDGE", "_lg.ESCALATE", "_lg.STOP"):
            self.assertIn(level, self.src)
        self.assertIn('stop_reason = "no_progress"', self.src)

    def test_recording_covers_blocked_and_denied_calls(self):
        # it reads back everything appended to actions_taken during the step, not just successes
        self.assertRegex(self.src, r"for _a in actions_taken\[n_actions_before:\]")

    def test_escalation_forces_main_and_resume_starts_on_main(self):
        self.assertIn("force_main_until", self.src)
        self.assertIn("AGENT_RESUME_PREFIX", self.src)
        self.assertRegex(self.src, r"forced_main = step < force_main_until")

    def test_a_main_first_run_stays_on_main(self):
        self.assertRegex(self.src, r"if main_first and force_main_until < steps:")

    def test_stop_reason_has_a_banner_summary_and_wrapup(self):
        self.assertRegex(self.src, r'"no_progress": "stopped: no progress')
        self.assertRegex(self.src, r'"no_progress", "max_steps", "timeout"\) and actions_taken')
        self.assertIn("'detail': loop_detail", self.src)

    def test_the_continue_prompt_matches_the_resume_prefix(self):
        from routes.agent.constants import AGENT_RESUME_PREFIX
        js = (RUN_PY.parents[2] / "static" / "js" / "agent-acts.js").read_text(encoding="utf-8")
        m = re.search(r"const AGENT_CONTINUE_PROMPT =\s*'([^']*)'", js)
        self.assertTrue(m and m.group(1).startswith(AGENT_RESUME_PREFIX))


class StopTextRoundTrips(unittest.TestCase):
    def test_server_and_banner_use_the_same_wording(self):
        from routes.agent.run import _describe_stop
        v = lg.Verdict(lg.STOP, "identical_result", "run_python", 3)
        self.assertEqual(_describe_stop(v.detail), lg.describe("identical_result", "run_python", 3))
        self.assertEqual(_describe_stop("garbage"), "the run repeated itself")


if __name__ == "__main__":
    unittest.main()


class RoutesAreBoundToTheirHandlers(unittest.TestCase):
    """A helper wedged between a decorator and its function silently re-binds the route to the
    helper (its parameters become required query fields: every request 422s)."""

    def test_agent_run_route_is_the_real_handler(self):
        from routes.agent import router
        by_path = {r.path: r for r in router.routes if getattr(r, "methods", None) and "POST" in r.methods}
        self.assertIn("/agent/run", by_path)
        self.assertEqual(by_path["/agent/run"].endpoint.__name__, "agent_run")

    def test_no_route_in_the_app_is_bound_to_a_private_helper(self):
        import server_manager
        private = [(r.path, r.endpoint.__name__) for r in server_manager.app.routes
                   if getattr(r, "endpoint", None) and r.endpoint.__name__.startswith("_")]
        self.assertEqual(private, [])
