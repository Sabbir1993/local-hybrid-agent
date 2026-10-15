"""Run journal: codes only, honest end states, restart reconciliation."""
import unittest
import uuid

from core import runs
from core.db import _usage_db


def rid():
    return "t-" + uuid.uuid4().hex[:12]


class RunJournal(unittest.TestCase):
    def test_begin_stage_end(self):
        r = rid()
        runs.begin(r, "chat", 7, session_id="s1", stage="ask")
        self.assertEqual(runs.get(r)["state"], "running")
        runs.stage(r, "build", step=3)
        row = runs.get(r)
        self.assertEqual((row["stage"], row["step"]), ("build", 3))
        runs.end(r, "completed", "done")
        self.assertEqual(runs.get(r)["state"], "completed")

    def test_a_final_state_is_never_rewritten(self):
        r = rid()
        runs.begin(r, "agent", 1)
        runs.end(r, "cancelled", "user")
        runs.end(r, "completed")
        runs.stage(r, "late")
        row = runs.get(r)
        self.assertEqual((row["state"], row["reason"], row["stage"]), ("cancelled", "user", None))

    def test_unknown_end_state_is_failed(self):
        r = rid()
        runs.begin(r, "chat", 1)
        runs.end(r, "weird")
        self.assertEqual(runs.get(r)["state"], "failed")

    def test_card_numbers_never_reach_the_journal(self):
        r = rid()
        runs.begin(r, "kb_ingest", 1, ref="invoice 4111 1111 1111 1111.pdf")
        runs.end(r, "failed", "bad card 4111111111111111")
        row = runs.get(r)
        self.assertNotIn("4111", (row["ref"] or "") + (row["reason"] or ""))

    def test_reconcile_marks_leftovers_interrupted(self):
        a, b = rid(), rid()
        runs.begin(a, "agent", 5)
        runs.begin(b, "agent", 5)
        runs.end(b, "completed")
        self.assertIn(a, [x["run_id"] for x in runs.open_runs(5)])
        runs.reconcile_interrupted(only=[a, b])
        self.assertEqual(runs.get(a)["state"], "interrupted")
        self.assertEqual(runs.get(a)["reason"], "server_restart")
        self.assertEqual(runs.get(b)["state"], "completed")
        self.assertIn(a, [x["run_id"] for x in runs.interrupted(5)])

    def test_reconcile_closes_dangling_route_runs(self):
        r = rid()
        _usage_db.execute("INSERT INTO route_runs (run_id, ts, user_id, mode) VALUES (?, 1, 1, 'agent')", (r,))
        _usage_db.commit()
        runs.reconcile_interrupted(only=[r])
        row = _usage_db.execute("SELECT outcome FROM route_runs WHERE run_id = ?", (r,)).fetchone()
        self.assertEqual(row[0], "interrupted")

    def tearDown(self):
        _usage_db.execute("DELETE FROM runs WHERE run_id LIKE 't-%'")
        _usage_db.execute("DELETE FROM route_runs WHERE run_id LIKE 't-%'")
        _usage_db.commit()


if __name__ == "__main__":
    unittest.main()
