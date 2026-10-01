import re
import sqlite3
import unittest
from pathlib import Path

from core import route_log
from core.agent_loop import PASSIVE_REFUSAL_NOTE, loop_guard as lg
from core.agent_loop.sandbox import validate_and_finalize_response

RUN_PY = Path(__file__).resolve().parent.parent / "routes" / "agent" / "run.py"


class PassiveRefusalIsRecorded(unittest.TestCase):
    """The model was asked to build something and replied by asking the user for the path
    or the code -- the stall small local models fall into. The detector existed but its
    result was returned as a `note` that no caller read, so a stalled run was indistinguishable
    from a normal answer in route_log. The text is still passed through unchanged; only the
    outcome code differs, which is what makes the signal measurable."""

    STALL = "Sure, I can do that. Please specify the exact file path you would like."

    def test_stall_is_flagged_when_the_user_asked_for_a_build(self):
        text, was_synth, note = validate_and_finalize_response(
            "create a landing page for my shop", self.STALL, "", [])
        self.assertEqual(note, PASSIVE_REFUSAL_NOTE)
        self.assertFalse(was_synth)
        self.assertEqual(text, self.STALL)          # the model's words are never rewritten

    def test_not_flagged_when_the_user_did_not_ask_for_a_build(self):
        _t, _s, note = validate_and_finalize_response(
            "what is the capital of France?", "Please specify the exact file path.", "", [])
        self.assertEqual(note, "validated")

    def test_not_flagged_when_the_agent_already_wrote_something(self):
        _t, _s, note = validate_and_finalize_response(
            "create a landing page", self.STALL, "",
            [{"name": "write_file", "ok": True, "args": {"path": "index.html"}}])
        self.assertNotEqual(note, PASSIVE_REFUSAL_NOTE)

    def test_a_normal_answer_is_untouched(self):
        text, was_synth, note = validate_and_finalize_response("what is 2+2?", "4", "", [])
        self.assertEqual((text, was_synth, note), ("4", False, "validated"))

    def test_run_end_records_the_stall_as_its_own_outcome(self):
        orig = route_log._usage_db
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE route_runs (run_id TEXT PRIMARY KEY, ts REAL NOT NULL, user_id INTEGER, "
                   "mode TEXT, category TEXT, steps INTEGER DEFAULT 0, outcome TEXT, "
                   "rating INTEGER, detail TEXT)")
        route_log._usage_db = db
        try:
            route_log.run_start("r1", None, "agent", "creation")
            # the code run.py runs once the answer is validated
            _text, was_synth, note = validate_and_finalize_response(
                "create a landing page", self.STALL, "", [])
            outcome = "synthesized" if was_synth else (
                "passive_refusal" if note == PASSIVE_REFUSAL_NOTE else "answered")
            route_log.run_end("r1", 2, outcome)
            got = db.execute("SELECT outcome FROM route_runs WHERE run_id='r1'").fetchone()[0]
            self.assertEqual(got, "passive_refusal")
            # and it stays out of the quality rollup's bad bucket: a stall is its own
            # signal, not a hard failure, and must not pollute the tuner's baseline
            self.assertNotIn("passive_refusal", route_log.BAD_OUTCOMES)
        finally:
            route_log._usage_db = orig


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
        # Was a source-text assertion on the two stop_reason->text dicts in run.py.
        # Those dicts moved to core/agent_loop/policy.py (stop_note/stop_headline) and are
        # now covered behaviorally in tests/test_policy.py:StopReasons (every member has
        # note+headline text, numbers interpolated, banner/summary agree). What remains
        # here is the wiring: the stopped-done event carries the loop detail through.
        self.assertIn("'detail': loop_detail", self.src)

    def test_the_continue_prompt_matches_the_resume_prefix(self):
        from routes.agent.constants import AGENT_RESUME_PREFIX
        js = (RUN_PY.parents[2] / "static" / "js" / "agent-acts.js").read_text(encoding="utf-8")
        m = re.search(r"const AGENT_CONTINUE_PROMPT =\s*'([^']*)'", js)
        self.assertTrue(m and m.group(1).startswith(AGENT_RESUME_PREFIX))


class StopTextRoundTrips(unittest.TestCase):
    def test_server_and_banner_use_the_same_wording(self):
        from core.agent_loop.policy import describe_stop as _describe_stop
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
