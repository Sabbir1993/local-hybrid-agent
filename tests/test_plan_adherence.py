"""tests/test_plan_adherence.py - does a run finish the plan it created?

RED for TIER1_EXECUTION_PLAN.md §2.2 follow-up. The §2.2 spec asks for a
structured task DAG because "the model cannot programmatically verify progress
against planned tasks" - but before adding a graph, note what is already there:
`plan_guard.check_update` already refuses to finish a step while an earlier one is
open, so plan ORDER is enforced. What is missing is any measurement of whether
runs actually complete their plans: `route_events.outcome` records what a turn
did, never whether the plan finished.

So this pins a run-level plan summary in `route_runs.detail` (codes, numbers
only - the same convention as err:/synth:/stall:). Without it, "should plans be a
DAG?" is unanswerable: there is no number for either answer.
"""
import sqlite3
import unittest
from pathlib import Path

from core.agent_loop import plan_guard
from core.db import db_get_plan_items


def items(*rows):
    """[(ord, text, status)] -> the dict shape db_get_plan_items returns."""
    return [{"ord": o, "text": t, "status": s, "note": None} for o, t, s in rows]


class RunSummaryTests(unittest.TestCase):
    def test_no_plan_records_nothing(self):
        self.assertIsNone(plan_guard.run_summary([]))

    def test_a_finished_plan_is_recorded(self):
        self.assertEqual(plan_guard.run_summary(items(
            (1, "a", "done"), (2, "b", "done"))), "plan:done")

    def test_open_steps_are_counted_not_just_flagged(self):
        code = plan_guard.run_summary(items((1, "a", "done"), (2, "b", "pending"), (3, "c", "pending")))
        self.assertEqual(code, "plan:open:2/3")

    def test_in_progress_counts_as_open(self):
        self.assertEqual(plan_guard.run_summary(items((1, "a", "done"), (2, "b", "in_progress"))),
                         "plan:open:1/2")

    def test_failed_steps_are_reported_alongside_open_ones(self):
        code = plan_guard.run_summary(items(
            (1, "a", "done"), (2, "b", "failed"), (3, "c", "pending")))
        self.assertEqual(code, "plan:failed:1/open:1/3")

    def test_a_fully_failed_plan_is_not_reported_as_done(self):
        self.assertEqual(plan_guard.run_summary(items((1, "a", "failed"), (2, "b", "failed"))),
                         "plan:failed:2")

    def test_unknown_status_is_not_silently_done(self):
        # an unrecognised status must not read as success
        self.assertEqual(plan_guard.run_summary(items((1, "a", "done"), (2, "b", "weird"))),
                         "plan:open:1/2")

    def test_code_is_bounded_and_carries_no_text(self):
        code = plan_guard.run_summary(items(
            (1, "a" * 4000, "done"), (2, "/home/alice/secret.txt done", "pending")))
        self.assertLessEqual(len(code), 24)
        self.assertNotIn("alice", code)
        self.assertNotIn("secret", code)


class ExistingEnforcementIsRealTests(unittest.TestCase):
    """The §2.2 premise says order is unenforced. It is not - pin what exists,
    so a future DAG change cannot quietly drop it."""

    def test_a_step_cannot_be_finished_while_an_earlier_one_is_open(self):
        cur = items((1, "a", "done"), (2, "b", "pending"), (3, "c", "pending"))
        err = plan_guard.check_update(cur, 3, "done")
        self.assertIsNotNone(err)
        self.assertIn("#2", err)

    def test_two_steps_cannot_be_in_progress_at_once(self):
        cur = items((1, "a", "in_progress"), (2, "b", "pending"))
        self.assertIsNotNone(plan_guard.check_update(cur, 2, "in_progress"))

    def test_marking_a_step_failed_is_always_allowed(self):
        cur = items((1, "a", "in_progress"), (2, "b", "pending"))
        self.assertIsNone(plan_guard.check_update(cur, 2, "failed"))


class DAGDependencyTests(unittest.TestCase):
    """Verifies that structured DAG dependencies are enforced when specified,
    allowing non-linear branches while blocking when declared deps are unfulfilled."""

    def test_dag_step_blocked_when_dependency_is_open(self):
        plan = [
            {"ord": 1, "text": "setup schema", "status": "done", "deps": []},
            {"ord": 2, "text": "create frontend", "status": "pending", "deps": [1]},
            {"ord": 3, "text": "backend api", "status": "pending", "deps": [2]},
        ]
        err = plan_guard.check_update(plan, 3, "in_progress")
        self.assertIsNotNone(err)
        self.assertIn("dependency step #2", err)

    def test_dag_step_allowed_when_dependencies_satisfied(self):
        # Step 3 depends only on Step 1 (parallel branch to Step 2).
        # Step 2 is still pending, but Step 3 should be allowed to start and finish.
        plan = [
            {"ord": 1, "text": "setup schema", "status": "done", "deps": []},
            {"ord": 2, "text": "independent docs", "status": "pending", "deps": [1]},
            {"ord": 3, "text": "apply migration", "status": "pending", "deps": [1]},
        ]
        self.assertIsNone(plan_guard.check_update(plan, 3, "in_progress"))
        self.assertIsNone(plan_guard.check_update(plan, 3, "done"))

    def test_dag_step_blocked_when_any_dep_is_not_done(self):
        plan = [
            {"ord": 1, "text": "schema", "status": "done", "deps": []},
            {"ord": 2, "text": "auth service", "status": "in_progress", "deps": [1]},
            {"ord": 3, "text": "integration test", "status": "pending", "deps": [1, 2]},
        ]
        err = plan_guard.check_update(plan, 3, "done")
        self.assertIsNotNone(err)
        self.assertIn("dependency step #2", err)


class VerificationGateTests(unittest.TestCase):
    """Verifies that programmatic verification gates block 'done' until concrete
    evidence (tests run, syntax clean, target files exist) is satisfied."""

    def test_gate_blocks_when_failing_files_exist(self):
        item = {"ord": 1, "text": "edit file", "phase": "act"}
        err = plan_guard.check_verification_gate(item, ".", [], failing_files=["bad.py"])
        self.assertIsNotNone(err)
        self.assertIn("syntax check", err)

    def test_gate_blocks_test_phase_without_test_run(self):
        item = {"ord": 2, "text": "run test suite", "phase": "test"}
        # only read_file actions taken, no run_tests or run_python
        acts = [{"name": "read_file", "ok": True}]
        err = plan_guard.check_verification_gate(item, ".", acts)
        self.assertIsNotNone(err)
        self.assertIn("no test/verification command was executed", err)

    def test_gate_passes_when_test_executed(self):
        item = {"ord": 2, "text": "run test suite", "phase": "test"}
        acts = [{"name": "run_tests", "ok": True}]
        err = plan_guard.check_verification_gate(item, ".", acts)
        self.assertIsNone(err)

    def test_gate_verifies_file_exists_requirement(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            item = {"ord": 3, "text": "build artifact", "verification": "file_exists:output.txt"}
            err = plan_guard.check_verification_gate(item, td, [{"name": "write_file", "ok": True}])
            self.assertIsNotNone(err)
            self.assertIn("does not exist", err)
            # Create the file
            (Path(td) / "output.txt").write_text("hello", encoding="utf-8")
            err2 = plan_guard.check_verification_gate(item, td, [{"name": "write_file", "ok": True}])
            self.assertIsNone(err2)


class DetailWiringTests(unittest.TestCase):
    def test_run_records_the_plan_summary(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from core import route_log
        from core.agent_loop import plan_guard as pg
        orig = route_log._usage_db
        route_log._usage_db = sqlite3.connect(":memory:")
        route_log._init()
        try:
            route_log.run_start("r1", 1, "m", "creation")
            route_log.run_end("r1", 4, "synthesized",
                              detail=pg.run_summary(items((1, "a", "done"), (2, "b", "pending"))))
            detail = route_log._usage_db.execute(
                "SELECT detail FROM route_runs WHERE run_id='r1'").fetchone()[0]
        finally:
            route_log._usage_db = orig
        self.assertEqual(detail, "plan:open:1/2")

    def test_the_loop_still_composes_with_other_codes(self):
        # the summary must not shadow a degeneration signal: both belong in detail
        src = "\n".join(
            p.read_text(encoding="utf-8")
            for p in (Path(__file__).resolve().parents[1] / "routes" / "agent" / "run.py",
                      Path(__file__).resolve().parents[1] / "routes" / "agent" / "stream.py"))
        self.assertIn("run_summary(", src, "run.py must record the plan summary")
        self.assertRegex(src, r"_outcome_detail or _plan_detail or loop_detail|\"\|\"\.join",
                         "codes must be composed, not overwritten")


if __name__ == "__main__":
    unittest.main()
