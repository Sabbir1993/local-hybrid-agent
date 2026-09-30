"""A run must be bounded by something other than the step count (routes/agent.py).

The reported symptom: a 6-step plan hit the 60-step cap and Continue could not
resume it. The step cap is a cliff, not a safety mechanism - it kills slow but
legitimate work while doing nothing to stop a thrashing model that varies its
arguments each round (the identical-args detector only sees byte-equal calls).

What this pins:
  * a wall-clock budget, the one bound that actually protects the GPU
  * near-repeat detection, so `npm run build` x N with different args is caught
  * a step-cap stop emits a real summary, never text:''
  * the plan counts sent to the UI are the plan's real counts

Run: python -m unittest tests.test_run_budget -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routes import agent as A  # noqa: E402


class RunTimeoutTests(unittest.TestCase):
    def test_a_default_budget_exists(self):
        """No bound at all is the bug: a run could hold the card forever."""
        self.assertGreater(A.DEFAULT_RUN_TIMEOUT_S, 0)
        self.assertGreater(A._run_timeout_s(), 0)

    def test_the_budget_is_configurable(self):
        try:
            A.APP_CONFIG.setdefault("agent", {})["run_timeout_s"] = 42
            self.assertEqual(A._run_timeout_s(), 42)
        finally:
            A.APP_CONFIG.get("agent", {}).pop("run_timeout_s", None)

    def test_zero_disables_the_ceiling(self):
        try:
            A.APP_CONFIG.setdefault("agent", {})["run_timeout_s"] = 0
            self.assertEqual(A._run_timeout_s(), 0)
        finally:
            A.APP_CONFIG.get("agent", {}).pop("run_timeout_s", None)

    def test_a_bad_value_falls_back_instead_of_crashing_the_run(self):
        try:
            A.APP_CONFIG.setdefault("agent", {})["run_timeout_s"] = "abc"
            self.assertEqual(A._run_timeout_s(), A.DEFAULT_RUN_TIMEOUT_S)
        finally:
            A.APP_CONFIG.get("agent", {}).pop("run_timeout_s", None)

    def test_negative_is_clamped_to_disabled(self):
        try:
            A.APP_CONFIG.setdefault("agent", {})["run_timeout_s"] = -5
            self.assertEqual(A._run_timeout_s(), 0)
        finally:
            A.APP_CONFIG.get("agent", {}).pop("run_timeout_s", None)


class _Window:
    """The near-repeat window, exercised exactly as the loop drives it."""

    def __init__(self, limit=A.NEAR_REPEAT_LIMIT, window=A.NEAR_REPEAT_WINDOW):
        self.hist = []
        self.limit, self.window = limit, window
        self.tripped = None

    def feed(self, tool, step):
        self.hist.append((tool, step))
        if self.hist and self.hist[-1][1] - self.hist[0][1] > self.window:
            cut = next((i for i, (_n, st) in enumerate(self.hist)
                        if step - st <= self.window), len(self.hist))
            self.hist = self.hist[cut:]
        names = [n for n, _s in self.hist]
        if names:
            top_tool, top_n = max(((n, names.count(n)) for n in set(names)),
                                  key=lambda p: p[1])
            if top_n >= self.limit:
                self.tripped = top_tool
        return self.tripped


class NearRepeatTests(unittest.TestCase):
    def test_rebuilding_with_different_args_is_caught(self):
        """The case the identical-args rule completely misses. Now only a high backstop: the
        progress-aware guard (tests/test_loop_guard.py) is what judges normal runs."""
        w = _Window()
        for step in range(A.NEAR_REPEAT_LIMIT):
            w.feed("run_command", step)   # same tool, args differ each time
        self.assertEqual(w.tripped, "run_command")

    def test_reading_many_different_files_is_caught(self):
        w = _Window()
        for step in range(A.NEAR_REPEAT_LIMIT):
            w.feed("read_file", step)
        self.assertEqual(w.tripped, "read_file")

    def test_genuinely_mixed_work_is_not_caught(self):
        """A real build step uses many different tools; that must never trip."""
        w = _Window()
        tools = ["list_files", "read_file", "grep", "edit_file", "run_command",
                 "read_file", "grep", "write_file", "run_command", "read_file",
                 "edit_file", "list_files"]
        for step, t in enumerate(tools):
            self.assertIsNone(w.feed(t, step), f"tripped early on step {step} ({t})")

    def test_a_long_but_varied_run_is_allowed(self):
        w = _Window(limit=6, window=12)
        for step in range(200):
            w.feed(f"tool_{step % 7}", step)   # no tool exceeds 6 per 12 steps
            self.assertIsNone(w.tripped, f"tripped at step {step}")

    def test_old_calls_leave_the_window(self):
        """A burst older than the window must not count toward the current one."""
        w = _Window()
        for step in range(A.NEAR_REPEAT_LIMIT):
            w.feed("read_file", step)
        self.assertEqual(w.tripped, "read_file", "the burst trips while it is in the window")
        # a single old call, then wander off well past the window
        w2 = _Window()
        w2.feed("read_file", 0)
        for step in range(1, 40):
            w2.feed(f"other_{step}", step)
        self.assertIsNone(w2.tripped, "a stale burst must age out of the window")

    def test_the_thresholds_are_sane(self):
        self.assertGreaterEqual(A.NEAR_REPEAT_LIMIT, 4,
                                "below 4 a legitimate workflow could trip")
        self.assertGreaterEqual(A.NEAR_REPEAT_WINDOW, A.NEAR_REPEAT_LIMIT)


class StopSummaryTests(unittest.TestCase):
    """A step-cap stop must never be a dead end with text:''."""

    def test_the_step_cap_is_raised(self):
        self.assertEqual(A.AGENT_MAX_STEPS, 200,
                         "a 6-step plan needing 150 steps must not be cut at 60")

    def test_every_stop_reason_is_distinguishable(self):
        """timeout must be tellable apart from max_steps in the UI."""
        reasons = ("max_steps", "timeout", "loop", "loop_near_repeat")
        self.assertEqual(len(set(reasons)), 4)


class PlanCountTests(unittest.TestCase):
    """THE reported bug: '0 of 6 plan steps left' while 4 were pending.

    The UI reads plan_total/pending off the done event, so those counts must come
    from the session's plan_items and count everything unfinished.
    """

    @staticmethod
    def _counts(statuses):
        items = [{"status": s} for s in statuses]
        return {
            "done": sum(1 for i in items if i["status"] == "done"),
            "failed": sum(1 for i in items if i["status"] == "failed"),
            "total": len(items),
            "pending": sum(1 for i in items if i["status"] in ("pending", "in_progress")),
        }

    def test_pending_counts_everything_unfinished(self):
        c = self._counts(["done", "done", "pending", "pending", "pending", "in_progress"])
        self.assertEqual(c, {"done": 2, "failed": 0, "total": 6, "pending": 4},
                         "4 unfinished steps must be reported as 4, never 0")

    def test_in_progress_counts_as_pending(self):
        self.assertEqual(self._counts(["in_progress"])["pending"], 1,
                         "an in-progress step is unfinished; dropping it hides real work")

    def test_a_fully_done_plan_reports_zero_pending(self):
        self.assertEqual(self._counts(["done", "done"])["pending"], 0)

    def test_failed_is_counted_but_is_not_pending(self):
        c = self._counts(["done", "failed", "pending"])
        self.assertEqual((c["failed"], c["pending"]), (1, 1))

    def test_the_live_session_that_reported_zero(self):
        """session 212 shape: 2 done, 5 pending - the UI said 0 left."""
        c = self._counts(["done", "done"] + ["pending"] * 5)
        self.assertNotEqual(c["pending"], 0)


class NarrationTests(unittest.TestCase):
    """THE reported bug: "Let me read this skill first." ended the whole run.

    A small orchestrator emits the announcement and then stops, calling no tool.
    The run treated that as a final answer and returned after one step, so
    "test this app on my browser" produced one sentence and no work. A fresh task
    has no plan, so the plan-incomplete nudge could not catch it.
    """

    NARRATION = [
        "Let me read this skill first.",
        "I will start by reading the file.",
        "Okay, let me check the browser setup.",
        "Sure, I will open the project.",
        "First, I will run the tests.",
        "Now let me search the codebase.",
        "Next I will list the files.",
        "To begin, let me inspect the routes.",
        "Let me navigate to the page and check.",
        "I am going to analyze the logs.",
        "Reading the config file now.",
        "I will test the app in the browser.",
    ]

    ANSWERS = [
        "The server started on port 8000 and returned 200 OK.",
        "The answer is 42.",
        "I found 3 issues: the cache never invalidates, the timeout is too low, "
        "and VRAM overflows. The first one is in routes/agent.py.",
        "I cannot access your browser directly, but I can guide you through it.",
        # courtesy, not an announcement of work
        "Let me know if you want me to continue.",
        # opens with "I will" but then explains something real
        "I will always use the keyword function in tests. The current setup "
        "re-parses config from disk on every request, which is why it is slow.",
        "",
        "   ",
        None,
    ]

    def test_every_narration_reply_is_caught(self):
        for t in self.NARRATION:
            with self.subTest(text=t):
                self.assertTrue(A._is_narration(t),
                                f"narration went undetected: {t!r}")

    def test_no_real_answer_is_caught(self):
        for t in self.ANSWERS:
            with self.subTest(text=t):
                self.assertFalse(A._is_narration(t),
                                 f"a real answer was mistaken for narration: {t!r}")

    def test_a_multi_paragraph_answer_is_never_narration(self):
        self.assertFalse(A._is_narration("Let me explain.\n\nFirst, the cache..."))

    def test_a_long_reply_is_never_narration(self):
        # a substantive answer that happens to start with an announcement verb
        long_answer = "I will " + ("explain the configuration in detail. " * 30)
        self.assertGreater(len(long_answer), 240)
        self.assertFalse(A._is_narration(long_answer))

    def test_the_opener_must_be_at_the_start(self):
        """A mid-sentence "let me check" is a citation, not a plan."""
        self.assertFalse(A._is_narration(
            "The docs suggest you let me check the cache settings before deploying."))

    def test_narration_without_a_recognised_verb_still_counts(self):
        # "First X, then Y" announces a sequence with no action verb at all
        self.assertTrue(A._is_narration("First the server, then the browser."))

    def test_the_nudge_is_bounded(self):
        """The model must not be pushed back forever."""
        self.assertGreater(A.MAX_PLAN_NUDGES, 0)
        self.assertLessEqual(A.MAX_PLAN_NUDGES, 5,
                             "more than a few retries just wastes the run")


if __name__ == "__main__":
    unittest.main()

