"""tests/test_synth_attribution.py - why did a run end "synthesized"?

RED for the early-exit investigation: core/agent_loop/sandbox.py already returns
a precise cause note ("synthesized response from successful tool actions",
"extracted answer from model reasoning", "fallback confirmation",
PASSIVE_REFUSAL_NOTE) and run.py streams it to the client as
`event: validated` - but route_runs.detail never received it. So all 16
recorded synthesized runs and all passive-refusal runs are indistinguishable in
usage.db: a stall the model refused to do, a fallback confirmation that invented
an answer, and a real synthesis from tool output are one bucket.

This pins the code mapping (fixed vocabulary, never the note text) and the
wiring at both outcome paths.
"""
import re
import sqlite3
import unittest
from pathlib import Path

from core import route_log
from core.agent_loop.sandbox import PASSIVE_REFUSAL_NOTE

RUN_PY = Path(__file__).resolve().parents[1] / "routes" / "agent" / "run.py"
STREAM_PY = Path(__file__).resolve().parents[1] / "routes" / "agent" / "stream.py"


class SynthDetailTests(unittest.TestCase):
    def _f(self):
        from routes.agent.run import _synth_detail
        return _synth_detail

    def test_every_sandbox_note_maps_to_a_code(self):
        f = self._f()
        cases = {
            "synthesized response from successful tool actions": "synth:tool_actions",
            "synthesized web search results": "synth:web_results",
            "synthesized execution output": "synth:execution",
            "extracted answer from model reasoning": "synth:reasoning_answer",
            "extracted full model reasoning": "synth:reasoning_full",
            "fallback confirmation": "synth:fallback_confirmation",
            PASSIVE_REFUSAL_NOTE: "stall:passive_refusal",
        }
        for note, want in cases.items():
            self.assertEqual(f(note), want, note)

    def test_a_normal_answer_records_nothing(self):
        # "validated" is the overwhelming majority of runs; a detail code for it
        # would bury the interesting rows
        self.assertIsNone(self._f()("validated"))

    def test_unknown_note_becomes_a_code_and_never_leaks_text(self):
        # a future note (or one carrying user content) must not reach the DB
        code = self._f()("synthesized something with /home/alice/secret.txt in it")
        self.assertEqual(code, "synth:other")
        self.assertNotIn("alice", code)
        self.assertTrue(re.fullmatch(r"[a-z_]+:[a-z_]+", code))

    def test_codes_are_bounded(self):
        for note in ("synthesized response from successful tool actions",
                     "x" * 5000, PASSIVE_REFUSAL_NOTE, "validated", None):
            code = self._f()(note)
            if code is not None:
                self.assertLessEqual(len(code), 40, note)


class SynthDetailIsWiredTests(unittest.TestCase):
    """Both outcome paths must record the cause, and the store must keep it."""

    def setUp(self):
        # both outcome paths moved verbatim to stream.py (2026-10-06); the
        # wiring guarantee is that the code exists in the agent call path,
        # whether it lives in run.py or stream.py
        self.src = "\n".join(p.read_text(encoding="utf-8")
                             for p in (RUN_PY, STREAM_PY))

    def test_both_finalize_paths_record_the_cause(self):
        # two call sites (the import carries no parens): the plan-mode path and
        # the main path. Each has to record the cause it just computed.
        self.assertEqual(self.src.count("validate_and_finalize_response("), 2)
        self.assertGreaterEqual(self.src.count("_outcome_detail = _synth_detail("), 2,
                                "a synthesized run that does not record its cause is the bug")

    def test_the_stored_detail_composes_rather_than_shadowing(self):
        # Codes must COMPOSE: a run can be both a synthesis and an unfinished
        # plan, and the degeneration signal must not be hidden by either. The
        # earlier `a or b or None` chain could only ever keep one of them.
        self.assertRegex(self.src, r"_detail = \"\|\"\.join\(",
                         "detail codes must be joined, not first-wins")
        self.assertIn("_outcome_detail, _plan_detail, loop_detail", self.src)

    def test_detail_reaches_the_database(self):
        orig = route_log._usage_db
        route_log._usage_db = sqlite3.connect(":memory:")
        route_log._init()
        try:
            from routes.agent.run import _synth_detail
            route_log.run_start("r1", 1, "m", "action")
            route_log.run_end("r1", 2, "synthesized",
                              detail=_synth_detail("fallback confirmation"))
            row = route_log._usage_db.execute(
                "SELECT outcome, detail FROM route_runs WHERE run_id='r1'").fetchone()
        finally:
            route_log._usage_db = orig
        self.assertEqual(row, ("synthesized", "synth:fallback_confirmation"))


if __name__ == "__main__":
    unittest.main()
