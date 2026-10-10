"""tests/test_swebench_harness.py - the SWE-bench grading core.

The operator run (scripts/run_swebench.py --live-*) needs a server, a model, a
Companion and a workspace; there is none in CI. What CI CAN pin is the part
that decides the score: the instance loader, the pytest-report parser and the
resolved-vs-regressed grader, plus the Wilson rollup. These are the same pieces
a human would eyeball before believing a published number.

Run: python -m unittest tests.test_swebench_harness -v
"""

import argparse
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import eval_mock  # noqa: F401,E402  (filters the tests dir onto sys.path the way the suites expect)
from scripts.run_swebench import (
    consume_sse,
    ensure_repo_cache,
    extract_patch,
    grade_patch,
    grade_resolved,
    load_instances,
    normalize_instance,
    parse_test_report,
    prepare_checkout,
    preflight_env,
    summarize,
    summarize_run,
    write_outputs,
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


class NormalizeAndParseTests(unittest.TestCase):
    def test_hf_style_string_lists_are_decoded(self):
        d = normalize_instance({"instance_id": "x", "FAIL_TO_PASS": '["a.py::t1"]', "PASS_TO_PASS": "[]"})
        self.assertEqual(d["FAIL_TO_PASS"], ["a.py::t1"])
        self.assertEqual(d["PASS_TO_PASS"], [])
        self.assertEqual(normalize_instance({"instance_id": "y"})["FAIL_TO_PASS"], [])

    def test_verbose_output_with_percent_column(self):
        out = ("tests/test_a.py::test_fix PASSED                      [ 50%]\n"
               "tests/test_a.py::test_p[1-a b] FAILED                [100%]\n")
        r = parse_test_report(out)
        self.assertEqual(r["passed"], ["tests/test_a.py::test_fix"])
        self.assertEqual(r["failed"], ["tests/test_a.py::test_p[1-a b]"])

    def test_rA_summary_lines_and_worst_outcome_wins(self):
        out = ("tests/test_a.py::test_x PASSED\n"
               "PASSED tests/test_a.py::test_x\n"
               "FAILED tests/test_a.py::test_x - assert 1 == 2\n")
        r = parse_test_report(out)
        self.assertEqual(r["passed"], [])
        self.assertEqual(r["failed"], ["tests/test_a.py::test_x"])


class ConsumeSseTests(unittest.TestCase):
    def test_folds_a_run(self):
        lines = ["event: run", 'data: {"run_id": "r9"}', "", "event: lane", 'data: {"lane": "executor"}',
                 "event: tool_call", 'data: {"name": "grep"}', "event: tool_call", 'data: {"name": "edit_file"}',
                 "event: delta", 'data: {"text": "fixed"}', "event: done",
                 'data: {"state": "completed"}', "event: delta", 'data: {"text": "ignored"}']
        r = consume_sse(lines)
        self.assertEqual((r["run_id"], r["tools"], r["final_text"], r["done_state"]),
                         ("r9", ["grep", "edit_file"], "fixed", "completed"))

    def test_failed_done_sets_error_and_garbage_is_skipped(self):
        r = consume_sse(["data: {}", "event: tool_call", "data: not-json", "event: done",
                         'data: {"state": "failed", "reason": "no_model"}'])
        self.assertEqual(r["tools"], [])
        self.assertIn("no_model", r["error"])


class SummarizeRunTests(unittest.TestCase):
    def test_env_errors_do_not_count_against_the_agent(self):
        recs = [{"status": "graded", "resolved": True, "patch_len": 5, "done_state": "completed", "tools": ["a"]},
                {"status": "graded", "resolved": False, "patch": "", "done_state": "stopped", "tools": []},
                {"status": "env_error", "resolved": None}]
        s = summarize_run(recs)
        self.assertEqual((s["instances"], s["graded"], s["env_errors"], s["resolved"]), (3, 2, 1, 1))
        self.assertEqual(s["resolved_rate"], 0.5)
        self.assertEqual(s["patch_rate"], 0.5)
        self.assertEqual(s["clean_termination_rate"], 0.5)


FAKE_RUNNER = '''
import pathlib, sys
if not pathlib.Path("tests/test_a.py").exists():
    print("no tests ran"); sys.exit(5)
src = pathlib.Path("calc.py").read_text()
print("tests/test_a.py::test_fix " + ("PASSED" if "a + b" in src else "FAILED") + " [ 50%]")
print("tests/test_a.py::test_keep " + ("FAILED" if "BROKEN" in src else "PASSED") + " [100%]")
'''
TEST_PATCH = ("diff --git a/tests/test_a.py b/tests/test_a.py\nnew file mode 100644\n"
              "--- /dev/null\n+++ b/tests/test_a.py\n@@ -0,0 +1 @@\n+# tests\n")


def _g(cwd, *a):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd,
                          capture_output=True, text=True, check=True).stdout.strip()


class GitFlowTests(unittest.TestCase):
    """Real git, a fake pytest-shaped runner: checkout, preflight, patch extraction, grading."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.origin = root / "origin"
        self.origin.mkdir()
        _g(self.origin, "init", "-q")
        (self.origin / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
        _g(self.origin, "add", "-A")
        _g(self.origin, "commit", "-q", "-m", "base")
        self.sha = _g(self.origin, "rev-parse", "HEAD")
        runner = root / "fake_runner.py"
        runner.write_text(FAKE_RUNNER, encoding="utf-8")
        self.cmd = f'"{sys.executable}" "{runner}"'
        self.work_root = root / "work"
        self.work_root.mkdir()
        self.inst = normalize_instance({
            "instance_id": "demo__demo-1", "repo": str(self.origin), "base_commit": self.sha,
            "problem_statement": "add() subtracts", "test_patch": TEST_PATCH,
            "FAIL_TO_PASS": ["tests/test_a.py::test_fix"], "PASS_TO_PASS": ["tests/test_a.py::test_keep"]})

    def tearDown(self):
        self.tmp.cleanup()

    def _checkout(self):
        return prepare_checkout(self.inst, ensure_repo_cache(self.inst["repo"], self.work_root / "_c"),
                                self.work_root)

    def test_preflight_accepts_a_gradable_instance_and_leaves_the_tree_clean(self):
        wd = self._checkout()
        self.assertIsNone(preflight_env(self.inst, wd, self.cmd, 60))
        self.assertEqual(_g(wd, "status", "--porcelain"), "")

    def test_preflight_rejects_when_tests_cannot_run(self):
        wd = self._checkout()
        why = preflight_env(self.inst, wd, f'"{sys.executable}" -c "print(1)"', 60)
        self.assertIn("no test results", why)

    def test_preflight_rejects_a_test_patch_that_does_not_apply(self):
        wd = self._checkout()
        bad = dict(self.inst, test_patch="diff --git a/nope b/nope\n--- a/nope\n+++ b/nope\n@@ -1 +1 @@\n-x\n+y\n")
        self.assertIn("does not apply", preflight_env(bad, wd, self.cmd, 60))

    def test_good_fix_is_resolved(self):
        wd = self._checkout()
        (wd / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        patch = extract_patch(wd)
        self.assertIn("+    return a + b", patch)
        self.assertTrue(grade_patch(self.inst, wd, self.cmd, 60)["resolved"])

    def test_fix_that_breaks_a_passing_test_is_not_resolved(self):
        wd = self._checkout()
        (wd / "calc.py").write_text("def add(a, b):\n    return a + b  # BROKEN\n", encoding="utf-8")
        self.assertFalse(grade_patch(self.inst, wd, self.cmd, 60)["resolved"])

    def test_untouched_checkout_has_an_empty_patch_and_new_files_are_included(self):
        wd = self._checkout()
        self.assertEqual(extract_patch(wd), "")
        (wd / "new_mod.py").write_text("x = 1\n", encoding="utf-8")
        (wd / "__pycache__").mkdir()
        (wd / "__pycache__" / "junk.pyc").write_bytes(b"\x00")
        patch = extract_patch(wd)
        self.assertIn("new_mod.py", patch)
        self.assertNotIn("junk.pyc", patch)

    def test_run_instance_end_to_end_with_a_stubbed_agent(self):
        import scripts.run_swebench as sw
        seen = {}

        def fake_point(base, client, pid, workdir):
            seen["wd"] = workdir
            return None

        def fake_agent(base, client, prompt, mode, max_steps, timeout_s):
            seen["prompt"] = prompt
            (seen["wd"] / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
            return {"run_id": "r1", "tools": ["edit_file"], "done_state": "completed", "done_reason": None,
                    "error": None}

        args = argparse.Namespace(base="http://x", test_cmd=self.cmd, test_timeout=60, mode="auto",
                                  max_steps=5, run_timeout=5, keep=False)
        ctx = {"client": None, "project_id": 1, "work_root": self.work_root, "cache_root": self.work_root / "_c"}
        with mock.patch.object(sw, "point_project_at", fake_point), mock.patch.object(sw, "run_agent", fake_agent):
            rec = sw.run_instance(self.inst, args, ctx)
        self.assertEqual((rec["status"], rec["resolved"], rec["done_state"]), ("graded", True, "completed"))
        self.assertIn("add() subtracts", seen["prompt"])
        self.assertNotIn("test_fix", seen["prompt"])            # the agent never sees the grading tests
        self.assertFalse(seen["wd"].exists())                   # checkout removed unless --keep

    def test_resume_keeps_earlier_predictions(self):
        out = Path(self.tmp.name) / "out"
        write_outputs(out, [{"instance_id": "a/1", "status": "graded", "resolved": True, "patch": "PATCH-A"}])
        write_outputs(out, [{"instance_id": "a/1", "status": "graded", "resolved": True, "patch_len": 7},
                            {"instance_id": "b/2", "status": "graded", "resolved": False, "patch": ""}])
        preds = [json.loads(x) for x in (out / "predictions.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual({p["instance_id"]: p["model_patch"] for p in preds}, {"a/1": "PATCH-A", "b/2": ""})


if __name__ == "__main__":
    unittest.main()
