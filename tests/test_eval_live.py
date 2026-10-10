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


class LivePreflightFailsFast(unittest.TestCase):
    """The two most common invocation mistakes must cost a second, not a 90-minute run.

    Before this existed, a server that was not listening raised an unhandled
    httpx.ConnectError traceback out of _live_login, and a server that was listening but
    rejecting the session made all 22 tasks x 5 repeats fail one timeout at a time.

    The preflight is reachability-only BY DESIGN: a 401 on an anonymous probe is expected
    (the probe carries no credentials) and says nothing about whether the supplied login
    will work. Gating on it here rejected every correctly-configured run.
    """

    def _patch_get(self, side_effect=None, status=None):
        from unittest import mock
        if side_effect is not None:
            return mock.patch("httpx.get", side_effect=side_effect)
        return mock.patch("httpx.get", return_value=mock.Mock(status_code=status))

    def test_connection_refused_gives_an_actionable_message_not_a_traceback(self):
        import httpx
        import eval_agent
        with self._patch_get(side_effect=httpx.ConnectError("refused")):
            with self.assertRaises(SystemExit) as cm:
                eval_agent._live_preflight("http://127.0.0.1:8000")
        self.assertEqual(cm.exception.code, 2,
                         "exit 2 = invocation error; exit 1 is reserved for agent task failure")

    def test_401_does_not_abort_the_preflight(self):
        """Regression guard: the preflight used to treat 401 as 'pass credentials', which is
        wrong for an anonymous probe. Credentials are the login step's business."""
        import eval_agent
        with self._patch_get(status=401):
            self.assertIsNone(eval_agent._live_preflight("http://127.0.0.1:8000"))
        with self._patch_get(status=403):
            self.assertIsNone(eval_agent._live_preflight("http://127.0.0.1:8000"))

    def test_other_http_errors_are_reported_not_swallowed(self):
        import httpx
        import eval_agent
        with self._patch_get(side_effect=httpx.ReadTimeout("slow")):
            with self.assertRaises(SystemExit) as cm:
                eval_agent._live_preflight("http://127.0.0.1:8000")
        self.assertEqual(cm.exception.code, 2)

    def test_reachable_server_passes(self):
        import eval_agent
        with self._patch_get(status=200):
            self.assertIsNone(eval_agent._live_preflight("http://127.0.0.1:8000"))

    def test_abort_prints_to_stderr_and_exits_2(self):
        import eval_agent
        with self.assertRaises(SystemExit) as cm:
            eval_agent._abort("boom")
        self.assertEqual(cm.exception.code, 2)

    def test_login_refused_does_not_leak_a_client_or_traceback(self):
        import httpx
        import eval_agent
        from unittest import mock
        with mock.patch.object(httpx, "Client") as C:
            C.return_value.post.side_effect = httpx.ConnectError("refused")
            with self.assertRaises(SystemExit) as cm:
                eval_agent._live_login("http://127.0.0.1:8000", "u", "p")
        self.assertEqual(cm.exception.code, 2)
        C.return_value.close.assert_called_once()


class LiveMfaLogin(unittest.TestCase):
    """The ticket flow (password -> single-use ticket -> ticket + code -> session), tested
    against scripted responses that mirror routes/auth.py. A benchmark runner cannot answer a
    TOTP prompt, which is why this fails fast with wiring for --live-totp / --live-totp-cmd
    rather than hanging."""

    def _client(self, posts):
        """httpx.Client stand-in whose post() returns canned responses in order.

        MagicMock, not Mock: _live_login arms the CSRF header via `client.headers[...] =`,
        which a bare Mock does not support - and that is exactly the surface under test.
        The cookie jar carries the CSRF cookie the real login would set.
        """
        import httpx
        from unittest import mock

        def _mk(status, body):
            r = mock.Mock()
            r.status_code = status
            r.json.return_value = body
            r.text = __import__("json").dumps(body)
            return r

        it = iter([_mk(s, b) for s, b in posts])
        C = mock.MagicMock()
        C.post.side_effect = lambda *a, **k: next(it)
        C.cookies.get.side_effect = lambda k: {"a770_csrf": "tok123"}.get(k)
        C.headers = {}
        return C

    def _login(self, client, totp=None):
        import eval_agent
        from unittest import mock
        import httpx
        with mock.patch.object(httpx, "Client", return_value=client):
            return eval_agent._live_login("http://x", "u", "p", totp=totp)

    def test_plain_login_without_mfa(self):
        c = self._client([(200, {"user": {"id": 1, "username": "u"}})])
        self.assertIs(self._login(c), c)

    def test_mfa_challenge_without_a_code_aborts_with_wiring(self):
        c = self._client([(200, {"mfa_required": True, "ticket": "mfa_abc"})])
        with self.assertRaises(SystemExit) as cm:
            self._login(c)
        self.assertEqual(cm.exception.code, 2)
        c.close.assert_called_once()

    def test_mfa_challenge_with_a_code_verifies_the_ticket(self):
        import json
        c = self._client([(200, {"mfa_required": True, "ticket": "mfa_abc"}),
                          (200, {"user": {"id": 1}, "ok": True})])
        self.assertIs(self._login(c, totp="123456"), c)
        verify = c.post.call_args_list[1]
        self.assertEqual(verify[0][0], "http://x/auth/mfa/verify")
        self.assertEqual(json.loads(json.dumps(verify[1]["json"])),
                         {"ticket": "mfa_abc", "code": "123456"})

    def test_wrong_code_aborts_without_retrying(self):
        """A wrong guess burns one of the ticket's 5 guesses server-side. The harness must
        NOT retry into the same ticket - it tells the operator to fetch a fresh code."""
        c = self._client([(200, {"mfa_required": True, "ticket": "mfa_abc"}),
                          (401, {"detail": "invalid code"})])
        with self.assertRaises(SystemExit) as cm:
            self._login(c, totp="000000")
        self.assertEqual(cm.exception.code, 2)
        self.assertEqual(c.post.call_count, 2)

    def test_enroll_required_points_at_the_ui(self):
        c = self._client([(200, {"mfa_required": True, "enroll_required": True,
                                 "ticket": "mfa_xyz"})])
        with self.assertRaises(SystemExit):
            self._login(c, totp="123456")

    def test_bad_password_aborts(self):
        c = self._client([(401, {"detail": "bad credentials"})])
        with self.assertRaises(SystemExit) as cm:
            self._login(c)
        self.assertEqual(cm.exception.code, 2)

    def test_no_user_and_no_challenge_is_impossible(self):
        c = self._client([(200, {})])
        with self.assertRaises(SystemExit):
            self._login(c)


class LiveTotpCommand(unittest.TestCase):
    def test_digits_are_extracted_and_padded(self):
        import eval_agent
        self.assertEqual(eval_agent._totp_code("echo 12345"), "012345")
        self.assertEqual(eval_agent._totp_code("echo code: 987654!"), "987654")

    def test_no_digits_aborts(self):
        import eval_agent
        with self.assertRaises(SystemExit) as cm:
            eval_agent._totp_code("echo nothing-here")
        self.assertEqual(cm.exception.code, 2)

    def test_unrunnable_command_aborts(self):
        import eval_agent
        with self.assertRaises(SystemExit) as cm:
            eval_agent._totp_code("definitely-not-a-real-command-xyz")
        # Windows runs through cmd: unknown command exits nonzero with empty stdout,
        # so this surfaces as "printed no digits" rather than a spawn error.
        self.assertEqual(cm.exception.code, 2)


class LiveCsrfArming(unittest.TestCase):
    """The first live smoke test read 0/3 because MFA worked and every task still 403'd:
    the harness logged in and then POSTed without the X-CSRF-Token echo the frontend
    sends. core/csrf.py rejects that. A benchmark client must behave like the browser."""

    def test_login_arms_the_csrf_header(self):
        import eval_agent
        from unittest import mock
        jar = {"a770_csrf": "tok123"}
        client = mock.Mock()
        client.cookies.get.side_effect = lambda k: jar.get(k)
        client.headers = {}
        eval_agent._arm_csrf(client, "http://x")
        self.assertEqual(client.headers.get("X-CSRF-Token"), "tok123")

    def test_missing_csrf_cookie_aborts_loudly(self):
        """A 200 without the cookie is an incomplete session. Proceeding would turn every
        task into a 403; aborting here names the actual problem."""
        import eval_agent
        from unittest import mock
        client = mock.Mock()
        client.cookies.get.return_value = None
        client.headers = {}
        with self.assertRaises(SystemExit) as cm:
            eval_agent._arm_csrf(client, "http://x")
        self.assertEqual(cm.exception.code, 2)

    def test_both_login_paths_arm_csrf(self):
        """Plain and MFA logins must both leave the client able to POST."""
        import eval_agent
        from unittest import mock
        with mock.patch.object(eval_agent, "_arm_csrf") as arm:
            c = mock.Mock()
            c.post.return_value = mock.Mock(
                status_code=200, json=lambda: {"user": {"id": 1}}, text="{}")
            import httpx
            with mock.patch.object(httpx, "Client", return_value=c):
                eval_agent._live_login("http://x", "u", "p")
            arm.assert_called_once_with(c, "http://x")

            c2 = mock.Mock()
            c2.post.side_effect = [
                mock.Mock(status_code=200,
                          json=lambda: {"mfa_required": True, "ticket": "mfa_t"}, text="{}"),
                mock.Mock(status_code=200, json=lambda: {"user": {"id": 1}}, text="{}"),
            ]
            with mock.patch.object(httpx, "Client", return_value=c2):
                eval_agent._live_login("http://x", "u", "p", totp="123456")
            arm.assert_called_with(c2, "http://x")
            self.assertEqual(arm.call_count, 2)


class LiveHttpErrorsDiagnoseThemselves(unittest.TestCase):
    """The second defect the smoke test exposed: HTTP-error exits `return rec` directly,
    skipping duration_s and the problems computation, so the run printed `Nones` and the
    operator had to read eval_results_live.json to learn it was a 403."""

    def _task(self):
        return {"name": "t", "prompt": "x"}

    def test_401_names_the_likely_causes(self):
        import eval_agent
        from unittest import mock
        resp = mock.Mock(status_code=401)
        stream = mock.MagicMock()
        stream.__enter__.return_value = resp
        client = mock.Mock()
        client.stream.return_value = stream
        rec = eval_agent.run_live_task("http://x", self._task(), "auto", client=client)
        self.assertTrue(rec["error"].startswith("HTTP 401"), rec["error"])
        self.assertIn("CSRF", rec["error"])
        self.assertIsNotNone(rec["duration_s"])
        self.assertTrue(rec["problems"])
        self.assertFalse(rec["ok"])

    def test_403_carries_the_gate_name_from_the_body(self):
        """The third defect the smoke test exposed: three different server-side gates return
        403 (native-app UA, Companion-app requirement, device workspace), and the harness
        dropped the body naming which one. Now it is captured."""
        import eval_agent
        import json
        from unittest import mock
        for gate in ("agent_native_only", "agent_requires_companion",
                     "agent_workspace_unavailable"):
            with self.subTest(gate):
                body = json.dumps({"error": gate,
                                   "message": f"refused: {gate}"}).encode()
                resp = mock.Mock(status_code=403)
                resp.read.return_value = body
                stream = mock.MagicMock()
                stream.__enter__.return_value = resp
                client = mock.Mock()
                client.stream.return_value = stream
                rec = eval_agent.run_live_task("http://x", self._task(), "auto",
                                               client=client)
                self.assertIn(gate, rec["error"], rec["error"])
                self.assertIsNotNone(rec["duration_s"])
                self.assertTrue(rec["problems"])
                self.assertFalse(rec["ok"])

    def test_403_with_empty_body_still_reports_duration(self):
        import eval_agent
        from unittest import mock
        resp = mock.Mock(status_code=403)
        resp.read.return_value = b""
        stream = mock.MagicMock()
        stream.__enter__.return_value = resp
        client = mock.Mock()
        client.stream.return_value = stream
        rec = eval_agent.run_live_task("http://x", self._task(), "auto", client=client)
        self.assertIn("empty body", rec["error"])
        self.assertIsNotNone(rec["duration_s"])

    def test_other_statuses_carry_duration_and_problems(self):
        import eval_agent
        from unittest import mock
        for status in (500, 502):
            with self.subTest(status):
                resp = mock.Mock(status_code=status)
                stream = mock.MagicMock()
                stream.__enter__.return_value = resp
                client = mock.Mock()
                client.stream.return_value = stream
                rec = eval_agent.run_live_task("http://x", self._task(), "auto", client=client)
                self.assertEqual(rec["error"], f"HTTP {status}")
                self.assertIsNotNone(rec["duration_s"])


class LiveStreamFailuresAreVisible(unittest.TestCase):
    """The third defect the smoke test exposed: the parser dropped the `done` payload, so a
    run that died in setup (done: failed) looked identical to a run where the model said
    nothing - `0.0s`, no tools, empty answer, no explanation. The failure reason must be
    captured, not just the fact of failure."""

    def _stream(self, frames):
        """A canned SSE stream: list of (event, data-dict) pairs."""
        import json
        from unittest import mock
        lines = []
        for event, data in frames:
            lines.append(f"event: {event}")
            lines.append(f"data: {json.dumps(data)}")
            lines.append("")
        resp = mock.Mock(status_code=200)
        resp.iter_lines.return_value = iter(lines)
        stream = mock.MagicMock()
        stream.__enter__.return_value = resp
        client = mock.Mock()
        client.stream.return_value = stream
        return client

    def _task(self):
        return {"name": "t", "prompt": "x"}

    def test_done_failed_captures_the_reason(self):
        import eval_agent
        client = self._stream([
            ("lane", {"lane": "executor"}),
            ("done", {"state": "failed", "reason": "timeout"}),
        ])
        rec = eval_agent.run_live_task("http://x", self._task(), "auto", client=client)
        self.assertEqual(rec["done_state"], "failed")
        self.assertIn("timeout", rec["error"])
        self.assertFalse(rec["ok"])

    def test_done_completed_with_no_tools_is_a_model_result_not_a_crash(self):
        import eval_agent
        client = self._stream([
            ("lane", {"lane": "executor"}),
            ("delta", {"text": "The answer is 42."}),
            ("done", {"state": "completed"}),
        ])
        rec = eval_agent.run_live_task("http://x", self._task(), "auto", client=client)
        self.assertEqual(rec["done_state"], "completed")
        self.assertIsNone(rec["error"])
        self.assertEqual(rec["final_text"], "The answer is 42.")

    def test_events_seen_records_the_stream_shape(self):
        import eval_agent
        client = self._stream([
            ("run", {"run_id": "abc"}),
            ("lane", {"lane": "executor"}),
            ("thought", {"text": "hmm"}),
            ("done", {"state": "completed"}),
        ])
        rec = eval_agent.run_live_task("http://x", self._task(), "auto", client=client)
        for e in ("run", "lane", "thought", "done"):
            self.assertIn(e, rec["events_seen"])

    def test_tool_call_events_are_counted(self):
        import eval_agent
        client = self._stream([
            ("lane", {"lane": "executor"}),
            ("tool_call", {"id": "t1", "name": "run_python", "args": {}}),
            ("delta", {"text": "3973"}),
            ("done", {"state": "completed"}),
        ])
        rec = eval_agent.run_live_task("http://x", self._task(), "auto", client=client)
        self.assertEqual(rec["tools"], ["run_python"])

    def test_guard_event_is_recorded(self):
        import eval_agent
        client = self._stream([
            ("lane", {"lane": "executor"}),
            ("guard", {"rule": "Never share personal info", "message": "blocked"}),
            ("done", {"state": "completed", "note": "response filtered by policy", "text": ""}),
        ])
        rec = eval_agent.run_live_task("http://x", self._task(), "auto", client=client)
        self.assertEqual(rec["guard_hits"], ["Never share personal info"])
        self.assertIn("filtered by policy", rec["error"])

    def test_kb_blocked_is_recorded(self):
        import eval_agent
        client = self._stream([
            ("kb_blocked", {"message": "cloud lane blocked"}),
            ("done", {"state": "completed"}),
        ])
        rec = eval_agent.run_live_task("http://x", self._task(), "auto", client=client)
        self.assertEqual(rec["kb_blocked"], "cloud lane blocked")


class SelfCheckRuns(unittest.TestCase):
    def test_module_self_check_passes(self):
        import subprocess
        r = subprocess.run([sys.executable, str(TESTS_DIR / "eval_stats.py")],
                           capture_output=True, text=True, timeout=180)
        self.assertEqual(r.returncode, 0, r.stdout[-2000:] + r.stderr[-2000:])
        self.assertIn("self-check OK", r.stdout)


class LiveProjectBootstrap(unittest.TestCase):
    """The 0/3 smoke-test defect: every task 403'd with
    agent_workspace_unavailable because no project was selected on the device.
    The harness must create + activate the eval project after login, exactly as
    the UI does, or every task fails identically before the loop starts."""

    def _client(self, projects=None, create_resp=None, activate_resp=None):
        import json
        from unittest import mock
        calls = []

        def _req(method, url, **kw):
            calls.append((method, url, kw.get("json")))
            if url.endswith("/control/projects") and method == "get":
                body = mock.Mock()
                body.status_code = 200
                body.json.return_value = {"projects": projects or []}
                return body
            if url.endswith("/control/projects") and method == "post":
                body = mock.Mock()
                body.status_code = 200
                body.json.return_value = create_resp or {"ok": True,
                                                         "project": {"id": 7, "name": "__eval__"}}
                return body
            if "/activate" in url and method == "post":
                body = mock.Mock()
                body.status_code = 200
                body.json.return_value = {"ok": True, "active": "__eval__"}
                return body
            if "/workspace" in url and method == "patch":
                body = mock.Mock()
                body.status_code = 200
                body.json.return_value = {"ok": True}
                return body
            body = mock.Mock()
            body.status_code = 404
            body.text = '{"error":"unseen ' + method + " " + url + '"}'
            return body

        client = mock.Mock()
        client.get.side_effect = lambda u, **kw: _req("get", u, **kw)
        client.post.side_effect = lambda u, **kw: _req("post", u, **kw)
        client.patch.side_effect = lambda u, **kw: _req("patch", u, **kw)
        return client, calls

    def test_creates_and_activates_project_when_missing(self):
        import eval_agent
        client, calls = self._client(projects=[])
        pid = eval_agent._live_ensure_project("http://x", client, "__eval__", "C:/eval_ws")
        self.assertEqual(pid, 7)
        self.assertIn(("post", "http://x/control/projects"),
                      [(m, u) for m, u, _ in calls])
        self.assertIn("__eval__", [q.get("name") for m, u, q in calls if q])
        self.assertIn("C:/eval_ws", [q.get("workspace_dir") for m, u, q in calls if q])
        self.assertIn(("post", "http://x/control/projects/7/activate"),
                      [(m, u) for m, u, _ in calls])

    def test_reuses_and_activates_existing_project(self):
        import eval_agent
        existing = [{"id": 7, "name": "__eval__", "workspace_dir": "C:/eval_ws"}]
        client, calls = self._client(projects=existing)
        pid = eval_agent._live_ensure_project("http://x", client, "__eval__", "C:/eval_ws")
        self.assertEqual(pid, 7)
        self.assertNotIn(("post", "http://x/control/projects"),
                         [(m, u) for m, u, _ in calls])
        self.assertIn(("post", "http://x/control/projects/7/activate"),
                      [(m, u) for m, u, _ in calls])

    def test_update_workspace_only_when_folder_missing_and_path_given(self):
        import eval_agent
        existing = [{"id": 7, "name": "__eval__", "workspace_dir": None}]
        client, calls = self._client(projects=existing)
        eval_agent._live_ensure_project("http://x", client, "__eval__", "C:/eval_ws")
        self.assertIn(("patch", "http://x/control/projects/7/workspace"),
                      [(m, u) for m, u, _ in calls])

    def test_activation_failure_aborts(self):
        import eval_agent
        from unittest import mock
        client = mock.Mock()
        client.get.return_value = mock.Mock(status_code=200,
                                            json=lambda: {"projects": [{"id": 7, "name": "__eval__",
                                                                        "workspace_dir": "C:/eval_ws"}]})
        client.post.return_value = mock.Mock(status_code=403, text='{"error":"no"}')
        with self.assertRaises(SystemExit) as e:
            eval_agent._live_ensure_project("http://x", client, "__eval__", "C:/eval_ws")
        # the message goes to stdout; SystemExit carries the code
        self.assertEqual(e.exception.code, 2)

    def test_preflight_pins_requests_to_the_companion_device(self):
        # Without X-Device-Id the server resolves the "default" device, which never
        # matches the companion's real device_id -> "different machine" 403.
        import eval_agent
        from unittest import mock
        client = mock.Mock()
        client.headers = {}
        client.get.return_value = mock.Mock(
            status_code=200,
            json=lambda: {"connected": True, "device_id": "dev-abc", "hostname": "WS-1"})
        eval_agent._live_preflight_companion("http://x", client, "eval")
        self.assertEqual(client.headers.get("X-Device-Id"), "dev-abc")
        self.assertEqual(client.headers.get("X-Device-Name"), "WS-1")


class LiveBaselineCompare(unittest.TestCase):
    def test_compare_summary_shapes_records_for_compare_baseline(self):
        import eval_agent
        records = [{"name": "a", "pass_rate": 0.8, "n": 5},
                   {"name": "b", "pass_rate": 1.0, "n": 3}]
        s = eval_agent._live_compare_summary(records)
        self.assertEqual(s["tasks"], {"a": {"pass_rate": 0.8, "n": 5},
                                      "b": {"pass_rate": 1.0, "n": 3}})
        self.assertIsNone(s["aggregate_rate"])
        self.assertIsNone(s["retrieval"])

    def test_pass_rate_drop_is_a_regression(self):
        from eval_mock import compare_baseline
        cur = {"tasks": {"a": {"pass_rate": 0.6, "n": 5}},
               "aggregate_rate": None, "retrieval": None,
               "router": None, "code": None}
        base = {"tasks": {"a": {"pass_rate": 1.0, "n": 5}}}
        problems = compare_baseline(cur, base)
        self.assertTrue(any("REGRESSION a" in p for p in problems))


if __name__ == "__main__":
    unittest.main()
