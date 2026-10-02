"""tests/test_eval_live.py - the live eval's graders and statistics.

An eval harness that is itself unverified is worse than none, because it produces confident
numbers. These tests pin the three things that make the live numbers trustworthy:

  1. check_record accepts a genuinely correct run and rejects each way a run can be wrong,
  2. the checker vocabulary in eval_tasks is coherent - no task asserts something impossible,
  3. eval_stats reports repeatability honestly rather than as a bare percentage.

Run: python -m unittest tests.test_eval_live -v
"""

import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))

import eval_stats
import eval_tasks


def _rec(**kw):
    base = {"tools": [], "lanes": [], "final_text": "", "final_len": 0,
            "ok": True, "error": None, "duration_s": 1.0, "problems": []}
    base.update(kw)
    return base


class CheckRecordAcceptsAGoodRun(unittest.TestCase):
    def test_a_correct_run_has_no_problems(self):
        task = dict(name="t", prompt="x", expect_tools=["write_file"],
                    expect_final_contains=["3973"])
        rec = _rec(tools=["write_file", "run_python"], final_text="The answer is 3973.")
        self.assertEqual(eval_tasks.check_record(task, rec), [])

    def test_no_checkers_still_rejects_an_empty_run(self):
        """A task with no checkers still must not pass on silence."""
        self.assertEqual(eval_tasks.check_record({"name": "t"},
                                                 _rec(tools=["list_files"], final_text="done")), [])
        self.assertTrue(eval_tasks.check_record({"name": "t"}, _rec()))


class CheckRecordCatchesEachFailureMode(unittest.TestCase):
    def test_missing_tool(self):
        task = {"name": "t", "expect_tools": ["write_file"]}
        problems = eval_tasks.check_record(task, _rec(tools=["read_file"]))
        self.assertTrue(any("missing tool: write_file" in p for p in problems))

    def test_forbidden_tool(self):
        task = {"name": "t", "forbid_tools": ["write_file"]}
        problems = eval_tasks.check_record(task, _rec(tools=["write_file"]))
        self.assertTrue(any("forbidden tool used" in p for p in problems))

    def test_answer_missing_the_required_fact(self):
        task = {"name": "t", "expect_final_contains": ["3973"]}
        problems = eval_tasks.check_record(
            task, _rec(tools=["run_python"], final_text="I ran the calculation."))
        self.assertTrue(any("missing '3973'" in p for p in problems), problems)

    def test_answer_calling_the_tool_is_not_the_answer(self):
        """The whole reason answer grading exists: a model that calls run_python and then
        answers with a haiku must not pass a task that asked for a number."""
        task = {"name": "t", "expect_tools": ["run_python"], "expect_final_contains": ["3973"]}
        rec = _rec(tools=["run_python"], final_text="Let me compute that for you!")
        self.assertTrue(any("missing '3973'" in p for p in eval_tasks.check_record(task, rec)))

    def test_answer_is_case_insensitive_for_contains(self):
        task = {"name": "t", "expect_final_contains": ["not found"]}
        self.assertEqual(eval_tasks.check_record(task, _rec(tools=["x"], final_text="Not Found.")), [])

    def test_answer_leaking_a_value_it_should_hold(self):
        """expect_final_lacks is the negative half. Without it the secret-holding task passes
        on a model that just repeats the key back."""
        task = {"name": "t", "expect_final_lacks": ["sk-proj-abc123"]}
        problems = eval_tasks.check_record(
            task, _rec(tools=["search_memory"], final_text="Sure: sk-proj-abc123"))
        self.assertTrue(any("must NOT contain" in p for p in problems))

    def test_too_few_distinct_tool_calls(self):
        task = {"name": "t", "expect_min_tools": 3}
        problems = eval_tasks.check_record(task, _rec(tools=["write_file", "write_file"]))
        self.assertTrue(any(">= 3 distinct" in p for p in problems))

    def test_silence_is_not_an_answer(self):
        problems = eval_tasks.check_record({"name": "t"}, _rec(tools=[], final_text=""))
        self.assertTrue(any("no tools and no answer" in p for p in problems))

    def test_transport_error_is_a_failure(self):
        problems = eval_tasks.check_record({"name": "t"}, _rec(error="HTTP 500"))
        self.assertTrue(any("transport error" in p for p in problems))


class FileChecks(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _specs(self, path, **kw):
        spec = {"path": path}
        spec.update(kw)
        return [{"path": path, **{k: v for k, v in kw.items() if k != "path"}}]

    def test_present_and_containing_passes(self):
        (self.ws / "a.txt").write_text("alpha beta", encoding="utf-8")
        task = {"name": "t", "expect_files": [{"path": "a.txt", "contains": "alpha beta"}]}
        self.assertEqual(eval_tasks.check_record(task, _rec(tools=["write_file"]), self.ws), [])

    def test_missing_file_fails(self):
        task = {"name": "t", "expect_files": [{"path": "nope.txt"}]}
        problems = eval_tasks.check_record(task, _rec(tools=["write_file"]), self.ws)
        self.assertTrue(any("missing file: nope.txt" in p for p in problems))

    def test_wrong_content_fails(self):
        (self.ws / "a.txt").write_text("gamma", encoding="utf-8")
        task = {"name": "t", "expect_files": [{"path": "a.txt", "contains": "alpha"}]}
        problems = eval_tasks.check_record(task, _rec(tools=["write_file"]), self.ws)
        self.assertTrue(any("does not contain 'alpha'" in p for p in problems))

    def test_not_contains_is_checked(self):
        (self.ws / "a.txt").write_text("alpha and delta", encoding="utf-8")
        task = {"name": "t", "expect_files": [{"path": "a.txt", "not_contains": ["delta"]}]}
        problems = eval_tasks.check_record(task, _rec(tools=["write_file"]), self.ws)
        self.assertTrue(any("must not contain 'delta'" in p for p in problems))

    def test_min_bytes_catches_an_empty_or_stub_file(self):
        (self.ws / "big.xlsx").write_bytes(b"tiny")
        task = {"name": "t", "expect_files": [{"path": "big.xlsx", "min_bytes": 3000}]}
        problems = eval_tasks.check_record(task, _rec(tools=["write_file"]), self.ws)
        self.assertTrue(any("expected >= 3000" in p for p in problems))

    def test_no_workspace_reports_skipped_r_than_silently_passing(self):
        """A remote server cannot be checked from here. Counting that as a pass would inflate
        every number in the file."""
        task = {"name": "t", "expect_files": [{"path": "a.txt"}]}
        problems = eval_tasks.check_record(task, _rec(tools=["write_file"]), None)
        self.assertTrue(any("skipped" in p for p in problems), problems)


class TaskSetIsCoherent(unittest.TestCase):
    def test_names_are_unique(self):
        names = [t["name"] for t in eval_tasks.TASKS]
        self.assertEqual(len(names), len(set(names)), "duplicate task names")

    def test_every_task_is_actually_asserted_something(self):
        """A task with no checkers always passes and teaches you nothing."""
        checkers = ("expect_tools", "forbid_tools", "expect_final_contains",
                    "expect_final_lacks", "expect_min_tools", "expect_files")
        for t in eval_tasks.TASKS:
            with self.subTest(t["name"]):
                self.assertTrue(any(t.get(k) for k in checkers),
                                f"{t['name']} asserts nothing")

    def test_no_task_asserts_a_tool_and_forbids_it(self):
        for t in eval_tasks.TASKS:
            with self.subTest(t["name"]):
                both = set(t.get("expect_tools", [])) & set(t.get("forbid_tools", []))
                self.assertFalse(both, f"{t['name']} both requires and forbids {both}")

    def test_expect_and_lacks_do_not_contradict(self):
        for t in eval_tasks.TASKS:
            with self.subTest(t["name"]):
                both = {s.lower() for s in t.get("expect_final_contains", [])} \
                    & {s.lower() for s in t.get("expect_final_lacks", [])}
                self.assertFalse(both, f"{t['name']} requires and forbids the same text {both}")

    def test_required_tool_must_not_be_forbidden(self):
        for t in eval_tasks.TASKS:
            with self.subTest(t["name"]):
                # a setup prompt is allowed to write even when the measured prompt may not
                for want in t.get("expect_tools", []):
                    self.assertNotIn(want, t.get("forbid_tools", []))

    def test_categories_are_declared_and_cover_the_interesting_ground(self):
        cats = set(eval_tasks.CATEGORIES)
        for need in ("files", "compute", "docs", "guard", "restraint", "planning"):
            self.assertIn(need, cats, f"no tasks in category {need}")

    def test_the_set_is_big_enough_to_be_a_measurement(self):
        """5 tasks cannot distinguish a capability from a coincidence."""
        self.assertGreaterEqual(len(eval_tasks.TASKS), 20)
        self.assertGreaterEqual(len([t for t in eval_tasks.TASKS if not t.get("soft")]), 15)

    def test_select_filters_by_name_and_category(self):
        by_name = eval_tasks.select(names=["compute_arithmetic"])
        self.assertEqual([t["name"] for t in by_name], ["compute_arithmetic"])
        comp = eval_tasks.select(categories=["compute"])
        self.assertTrue(comp and all(t["category"] == "compute" for t in comp))
        no_soft = eval_tasks.select(categories=["retrieval"], include_soft=False)
        self.assertTrue(all(not t.get("soft") for t in no_soft))

    def test_unknown_task_name_is_rejected_loudly(self):
        with self.assertRaises(SystemExit):
            eval_tasks.select(names=["no_such_task"])


class StatisticsAreHonest(unittest.TestCase):
    def test_flaky_is_distinguished_from_stable_failure(self):
        s = eval_stats.summarize_task("t", [{"ok": True}, {"ok": True}, {"ok": False}])
        self.assertTrue(s["flaky"])
        self.assertFalse(s["stable"])
        self.assertAlmostEqual(s["pass_rate"], 0.667, places=3)

    def test_all_pass_is_stable_not_flaky(self):
        s = eval_stats.summarize_task("t", [{"ok": True}] * 5)
        self.assertTrue(s["stable"])
        self.assertFalse(s["flaky"])

    def test_all_fail_is_stable_not_flaky(self):
        s = eval_stats.summarize_task("t", [{"ok": False}] * 4)
        self.assertEqual(s["n_pass"], 0)
        self.assertTrue(s["stable"])
        self.assertFalse(s["flaky"])

    def test_wilson_interval_stays_inside_the_unit_range(self):
        """At n=5 the normal approximation can produce an interval outside [0,1], which is
        how 3/5 gets reported as a range that excludes 3/5."""
        for n_ok, n in ((0, 5), (1, 5), (3, 5), (4, 5), (5, 5)):
            with self.subTest(f"{n_ok}/{n}"):
                lo, hi = eval_stats.wilson_ci(n_ok, n)
                self.assertGreaterEqual(lo, 0.0)
                self.assertLessEqual(hi, 1.0)
                self.assertLessEqual(lo, hi)

    def test_interval_brackets_the_observed_rate(self):
        for n_ok, n in ((1, 5), (3, 5), (4, 5)):
            lo, hi = eval_stats.wilson_ci(n_ok, n)
            self.assertLessEqual(lo, n_ok / n)
            self.assertGreaterEqual(hi, n_ok / n)

    def test_more_repeats_narrow_the_interval(self):
        lo1, hi1 = eval_stats.wilson_ci(4, 5)
        lo2, hi2 = eval_stats.wilson_ci(40, 50)
        self.assertLess((hi2 - lo2), (hi1 - lo1))

    def test_errors_count_as_failures_not_dropped_runs(self):
        s = eval_stats.summarize_task("t", [{"ok": True, "error": None},
                                            {"ok": False, "error": "timeout"}])
        self.assertEqual((s["n_pass"], s["n"]), (1, 2))
        self.assertTrue(any("transport error" in p for p in s["problems"]))

    def test_rollup_separates_gating_from_soft(self):
        recs = [eval_stats.summarize_task("hard", [{"ok": True}], soft=False),
                eval_stats.summarize_task("soft", [{"ok": False}], soft=True)]
        roll = eval_stats.rollup(recs)
        self.assertEqual(roll["hard_tasks"], 1)
        self.assertEqual(roll["hard_pass_rate"], 1.0)
        self.assertEqual(roll["pass_rate"], 0.5)
        self.assertEqual(roll["failing_tasks"], ["soft"])

    def test_rollup_lists_flaky_separately_from_failing(self):
        recs = [eval_stats.summarize_task("flaky", [{"ok": True}, {"ok": False}]),
                eval_stats.summarize_task("dead", [{"ok": False}, {"ok": False}])]
        roll = eval_stats.rollup(recs)
        self.assertEqual(roll["failing_tasks"], ["dead"])
        self.assertEqual(roll["flaky_tasks"], ["flaky"])

    def test_by_category_rollup(self):
        recs = [eval_stats.summarize_task("a", [{"ok": True}], category="files"),
                eval_stats.summarize_task("b", [{"ok": True}], category="files"),
                eval_stats.summarize_task("c", [{"ok": True}], category="compute")]
        roll = eval_stats.rollup(recs)
        self.assertEqual(roll["by_category"]["files"]["tasks"], 2)
        self.assertEqual(roll["by_category"]["files"]["pass_rate"], 1.0)
        self.assertEqual(roll["by_category"]["compute"]["pass_rate"], 1.0)

    def test_report_shows_the_interval_next_to_the_rate(self):
        recs = [eval_stats.summarize_task("t", [{"ok": True}, {"ok": False}])]
        text = eval_stats.format_report(eval_stats.rollup(recs), recs)
        self.assertIn("ci95", text)
        self.assertIn("FLAKY", text.upper())
        # a bare percentage is exactly what this module exists to stop printing alone
        self.assertRegex(text, r"pass rate\s+\d\.\d+\s+ci95")

    def test_dedupes_repeated_problem_strings(self):
        runs = [{"ok": False, "problems": ["missing tool: write_file"]},
                {"ok": False, "problems": ["missing tool: write_file"]}]
        s = eval_stats.summarize_task("t", runs)
        self.assertEqual(s["problems"].count("missing tool: write_file"), 1)


class SelfCheckRuns(unittest.TestCase):
    def test_module_self_check_passes(self):
        import subprocess
        r = subprocess.run([sys.executable, str(TESTS_DIR / "eval_stats.py")],
                           capture_output=True, text=True, timeout=180)
        self.assertEqual(r.returncode, 0, r.stdout[-2000:] + r.stderr[-2000:])
        self.assertIn("self-check OK", r.stdout)


if __name__ == "__main__":
    unittest.main()