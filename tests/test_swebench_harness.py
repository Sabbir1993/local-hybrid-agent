"""tests/test_swebench_harness.py - the SWE-bench grading core.

The operator run (scripts/run_swebench.py --live-*) needs a server, a model, a
Companion and a workspace; there is none in CI. What CI CAN pin is the part
that decides the score: the instance loader, the pytest-report parser and the
resolved-vs-regressed grader, plus the Wilson rollup. These are the same pieces
a human would eyeball before believing a published number.

Run: python -m unittest tests.test_swebench_harness -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import eval_mock  # noqa: F401,E402  (filters the tests dir onto sys.path the way the suites expect)
from scripts.run_swebench import (
    grade_resolved,
    load_instances,
    parse_test_report,
    summarize,
)


def _instance(f2p=("tests/test_a.py::test_fix",), p2p=("tests/test_a.py::test_keep",)):
    return {"instance_id": "repo-1_issue-1",
            "problem_statement": "Fix the bug.",
            "FAIL_TO_PASS": list(f2p),
            "PASS_TO_PASS": list(p2p)}


class LoadInstancesTests(unittest.TestCase):
    def test_parses_jsonl_and_normalises(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "instances.jsonl"
            p.write_text(json.dumps(_instance()) + "\n" + json.dumps(_instance("t2")) + "\n",
                         encoding="utf-8")
            loaded = load_instances(p)
        self.assertEqual([i["instance_id"] for i in loaded],
                         ["repo-1_issue-1", "repo-1_issue-1"])
        self.assertIn("FAIL_TO_PASS", loaded[0])

    def test_skips_bad_rows_without_killing_the_run(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "instances.jsonl"
            p.write_text("not json\n" + json.dumps({}) + "\n" + json.dumps(_instance()) + "\n",
                         encoding="utf-8")
            loaded = load_instances(p)
        self.assertEqual(len(loaded), 1)


class ParseTestReportTests(unittest.TestCase):
    def test_extracts_names_and_pass_fail(self):
        out = ("tests/test_a.py::test_fix PASSED\n"
               "tests/test_a.py::test_keep PASSED\n"
               "tests/test_b.py::test_broken FAILED\n")
        r = parse_test_report(out)
        self.assertEqual(r["passed"], ["tests/test_a.py::test_fix",
                                       "tests/test_a.py::test_keep"])
        self.assertEqual(r["failed"], ["tests/test_b.py::test_broken"])

    def test_ignores_summary_noise(self):
        out = ("tests/test_a.py::test_fix PASSED\n"
               "2 passed, 0 failed in 0.03s\n"
               "== short test summary ==\n")
        r = parse_test_report(out)
        self.assertEqual(r["passed"], ["tests/test_a.py::test_fix"])


class GradeResolvedTests(unittest.TestCase):
    def test_resolved_when_all_f2p_pass_and_p2p_survive(self):
        inst = _instance()
        self.assertTrue(grade_resolved(inst, {"tests/test_a.py::test_fix",
                                               "tests/test_a.py::test_keep"}))

    def test_unresolved_when_a_f2p_test_still_fails(self):
        inst = _instance()
        self.assertFalse(grade_resolved(inst, {"tests/test_a.py::test_keep"}))

    def test_regressed_p2p_is_unresolved(self):
        inst = _instance()
        # fix passed, keep regressed (not in observed pass set)
        self.assertFalse(grade_resolved(inst, {"tests/test_a.py::test_fix"}))

    def test_no_evidence_returns_none(self):
        self.assertIsNone(grade_resolved(_instance(f2p=(), p2p=()), set()))


class SummarizeTests(unittest.TestCase):
    def test_rollup_counts_resolved(self):
        a, b = _instance(), _instance()
        a["_resolved"] = True
        b["_resolved"] = False
        s = summarize([a, b])
        self.assertEqual(s["resolved"], 1)
        self.assertEqual(s["pass_rate"], 0.5)


if __name__ == "__main__":
    unittest.main()
