"""tests/test_subagent_scope_leak.py - the sub-agent ContextVar must always be restored,
and the critic gate must fail closed.

run_subagent set _active_scope_writes to a fresh list and reset it with a bare statement
AFTER the work block. The `return "error: no model is available ..."` path exited before that
statement ran, and because run_subagent is awaited in the CALLER's context (not its own task),
the orphaned list stayed installed for the rest of the parent run. blackboard_post is
deliberately not in DENIED_TOOLS, so the parent could keep appending into that dead list.

parse_reviewer_verdict used to end with `return True, "No critical defects flagged by
reviewer"`, so any reviewer output naming no defect phrase and no approval keyword was
APPROVED - a 500-word critique that never typed "VERDICT:" sailed through the gate.

Run: python -m unittest tests.test_subagent_scope_leak -v
"""

import asyncio
import contextlib
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.subagent import runner as sr
from core.subagent.blackboard import _active_scope_writes


async def _empty_awaitable(*_a, **_k):
    return None


def _no_lanes():
    """Make every lane unavailable, so run_subagent takes the `client is None` early return.

    Deliberately does NOT patch `core.lanes.targets`: the route is not always built from it.
    runner.py:130 takes the lane from the role's own config ("coder" -> executor), and when
    that resolves it builds the route from the registry instead, so patching `targets` alone
    leaves a real lane in place - which then makes a live model call and takes ten seconds.
    Making Target.available() False is route-shape-independent.
    """
    return (
        mock.patch("core.agent_tools.active_workspace", return_value="C:/ws"),
        mock.patch("core.project_context.load_project_instructions", new=_empty_awaitable()),
        mock.patch("core.project_context.prompt_block", return_value=""),
        mock.patch("core.lanes.Target.available", return_value=False),
        mock.patch("core.cloud.role_map", return_value={}),
        mock.patch("core.cloud.cloud_lane", return_value=None),
    )


class ScopeIsRestoredOnEveryExit(unittest.TestCase):
    """Force the "no model is available" early return: no routed targets, no cloud lane."""

    def _run_with(self, extra=()):
        patches = list(_no_lanes()) + list(extra)
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            return asyncio.run(sr.run_subagent(task="do a thing", role="coder"))

    def _sentinel(self):
        sentinel = ["parent-owned"]
        _active_scope_writes.set(sentinel)
        self.addCleanup(_active_scope_writes.set, [])
        return sentinel

    def test_the_early_return_really_is_reached(self):
        """Guards the other tests: if run_subagent stops early for another reason, the
        restoration assertions below would pass vacuously."""
        self._sentinel()
        out = self._run_with()
        self.assertTrue(out.startswith("error:"), f"expected the no-model error, got {out[:90]!r}")
        self.assertIn("no model is available", out)

    def test_restored_on_the_early_return_path(self):
        sentinel = self._sentinel()
        self._run_with()
        self.assertIs(_active_scope_writes.get(), sentinel,
                      "ContextVar not restored on the early-return path")

    def test_child_writes_are_not_leaked_into_the_parent(self):
        """The observable consequence: the parent's list is the same object, unchanged."""
        sentinel = self._sentinel()
        self._run_with()
        self.assertIs(_active_scope_writes.get(), sentinel)
        self.assertEqual(list(_active_scope_writes.get()), ["parent-owned"])

    def test_restored_when_the_body_raises(self):
        sentinel = self._sentinel()
        boom = RuntimeError("lane resolution exploded")
        with self.assertRaises(RuntimeError):
            self._run_with([mock.patch("core.lanes.Target.describe", side_effect=boom)])
        self.assertIs(_active_scope_writes.get(), sentinel,
                      "ContextVar not restored when the body raises")


class CriticGateFailsClosed(unittest.TestCase):
    """Silence is not approval, and an explicit defect term outranks an approval word."""

    def _v(self, text):
        from core.subagent.critic import parse_reviewer_verdict
        return parse_reviewer_verdict(text)

    def test_unparseable_review_is_rejected(self):
        approved, why = self._v(
            "The function reads the config twice, which is mildly wasteful but works.\n"
            "I considered naming the variable better. Overall it behaves as intended.")
        self.assertFalse(approved, "silence must not be read as approval")
        self.assertIn("cannot be trusted", why)

    def test_explicit_approval_passes(self):
        self.assertTrue(self._v("VERDICT: APPROVED")[0])

    def test_explicit_rejection_fails(self):
        self.assertFalse(self._v("VERDICT: REJECTED - off by one")[0])

    def test_lgtm_passes(self):
        self.assertTrue(self._v("LGTM")[0])

    def test_changes_required_fails(self):
        self.assertFalse(self._v("CHANGES REQUIRED before merge")[0])

    def test_defect_terms_fail(self):
        for t in ("there is a syntax error on line 4",
                  "this is a regression risk",
                  "possible vulnerability in the parser"):
            with self.subTest(t[:28]):
                self.assertFalse(self._v(t)[0])

    def test_empty_is_rejected(self):
        self.assertFalse(self._v("")[0])

    def test_defect_term_outranks_an_approval_word(self):
        """Regression guard: the approval check used to run first, which made the defect
        heuristic unreachable whenever the review also said 'APPROVED'."""
        approved, why = self._v("Mostly APPROVED, but this is a regression.")
        self.assertFalse(approved)
        self.assertIn("regression", why)

    def test_clean_approval_language_still_passes(self):
        self.assertTrue(self._v("Looks good to me - APPROVED")[0])


if __name__ == "__main__":
    unittest.main()