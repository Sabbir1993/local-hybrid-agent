"""tests/test_subagent_envelope.py - a sub-agent's outcome must be visible, not guessed.

RED for T2 (TIER1_EXECUTION_PLAN.md Gap 1). Two defects, one measurement and
one correctness:

* The parent's verdict on a tool call is `ok = not result.startswith("error:")`
  (routes/agent/run.py). A sub-agent's result is prefixed with a
  `[sub-agent · ...]` header, so it never starts with "error:" - meaning a child
  that hit its step limit was recorded to telemetry, and shown to the parent,
  as SUCCESS. The plan document claims the opposite (that bad envelopes make
  the parent think the call failed); the code does the reverse.
* The five recorded `spawn_agent` failures are all early returns
  (core/subagent/runner.py:66 empty task, :80 unknown role, :143 no model) and
  all collapse to the same `"error"` code in route_log, so the cause is
  invisible.

What is pinned here:
  * the envelope carries a machine-readable status the parent can act on
  * step_exhausted / no_response are failures; success is not
  * "error:" text INSIDE a successful child's answer must not flip the verdict
  * the classifier distinguishes the three early returns
"""
import unittest

from core.subagent import runner as subrunner


def verdict(name, result):
    """The parent-side verdict helper (routes/agent/run.py consults this)."""
    return subrunner.subagent_result_verdict(name, result)


class EnvelopeStatusTests(unittest.TestCase):
    def test_success_envelope_is_ok(self):
        ok = "[sub-agent · role=code-reviewer · 6 msgs · lane=executor · status=success]\nlooks fine"
        self.assertIs(verdict("spawn_agent", ok), True)

    def test_step_exhausted_envelope_is_a_failure(self):
        ex = ("[sub-agent · role=code-reviewer · 8 msgs · lane=executor · status=step_exhausted]\n"
              "(sub-agent reached its step limit without a final answer)")
        self.assertIs(verdict("spawn_agent", ex), False)

    def test_no_response_envelope_is_a_failure(self):
        nr = "[sub-agent · lane=main · status=no_response]\n(sub-agent got no response from the model)"
        self.assertIs(verdict("spawn_agent", nr), False)

    def test_error_text_inside_a_successful_child_does_not_flip_it(self):
        # the child legitimately quoted an error string in its findings
        ok = ("[sub-agent · role=code-reviewer · 6 msgs · lane=executor · status=success]\n"
              "error: the old code did not compile - here is the fix")
        self.assertIs(verdict("spawn_agent", ok), True,
                      "body text must not decide the verdict; the status field does")

    def test_quoted_envelope_in_body_does_not_flip_a_single_child(self):
        # a successful child quoted another agent's envelope line in its
        # findings. Only the FIRST line is the child's own envelope - the body
        # may not vote. Regression for the scan-all-lines verdict window.
        ok = ("[sub-agent · role=code-reviewer · 6 msgs · lane=executor · status=success]\n"
              "The sibling [sub-agent · lane=main · status=step_exhausted] gave up, but the fix is here.")
        self.assertIs(verdict("spawn_agent", ok), True,
                      "a quoted envelope in the body must not flip a single child's verdict")

    def test_quoted_envelope_in_body_for_reviewed_coder(self):
        ok = ("[critic-actor · iterations=1/2 · verdict=APPROVED · status=success]\n"
              "Reviewer hit [sub-agent · status=step_exhausted] earlier but recovered.")
        self.assertIs(verdict("spawn_reviewed_coder", ok), True)

    def test_non_subagent_tools_are_left_to_the_caller(self):
        self.assertIsNone(verdict("read_file", "a = 1\n"))
        self.assertIsNone(verdict("spawn_agent", "error: unknown role 'x'. Available: a, b"))

    def test_parallel_envelope_verdict_is_the_worst_child(self):
        mixed = ("[Sub-agent #1]: [sub-agent · status=success]\nok\n"
                 "[Sub-agent #2]: [sub-agent · status=step_exhausted]\nnope")
        self.assertIs(verdict("spawn_parallel_agents", mixed), False)


class EnvelopeShapeTests(unittest.TestCase):
    def test_header_carries_role_lane_steps_and_status(self):
        h = subrunner._envelope_header(role="code-reviewer", lane="executor",
                                        n_msgs=8, status="success")
        self.assertIn("role=code-reviewer", h)
        self.assertIn("lane=executor", h)
        self.assertIn("8 msgs", h)
        self.assertIn("status=success", h)
        self.assertTrue(h.startswith("[sub-agent"), h)

    def test_header_without_role_stays_valid(self):
        h = subrunner._envelope_header(role=None, lane="main", n_msgs=2, status="success")
        self.assertIn("lane=main", h)
        self.assertNotIn("role=", h)
        self.assertIs(verdict("spawn_agent", h + "\ndone"), True)

    def test_only_known_statuses_are_accepted(self):
        # an unrecognised status must not be read as success
        weird = "[sub-agent · lane=main · status=banana]\nx"
        self.assertIsNone(verdict("spawn_agent", weird),
                          "unknown status is not a verdict - fail closed to the caller")


class FailureCodeTests(unittest.TestCase):
    """The three early returns must be distinguishable in route_log."""

    def test_each_spawn_failure_gets_its_own_code(self):
        from core import route_log
        cases = {
            "error: spawn_agent requires a non-empty task": "invalid_args",
            "error: unknown role 'code-rev'. Did you mean 'code-reviewer'? Available: a, b": "unknown_role",
            "error: no model is available for this sub-agent task (role=generic). Tried: executor.": "no_model",
        }
        for text, want in cases.items():
            self.assertEqual(route_log.classify_tool_result(text), want, text[:50])

    def test_codes_carry_no_free_text(self):
        from core import route_log
        code = route_log.classify_tool_result(
            "error: no model is available for this sub-agent task (role=x). Tried: /home/alice/models")
        self.assertEqual(code, "no_model")
        self.assertNotIn("alice", code)


if __name__ == "__main__":
    unittest.main()