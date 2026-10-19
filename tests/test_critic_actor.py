import asyncio
import unittest
from unittest import mock

from core.subagent import (
    DENIED_TOOLS,
    get_workspace_changes_diff,
    parse_reviewer_verdict,
    run_critic_actor_cycle,
    tool_spawn_reviewed_coder,
)
from core.subagent.runner import subagent_result_verdict


class TestCriticActor(unittest.TestCase):
    def test_denied_tools_contains_spawn_reviewed_coder(self):
        self.assertIn("spawn_reviewed_coder", DENIED_TOOLS)

    def test_parse_reviewer_verdict_approved(self):
        cases = [
            ("VERDICT: APPROVED", True, "Approved"),
            ("VERDICT: APPROVED - All unit tests pass", True, "All unit tests pass"),
            ("VERDICT: PASS", True, "Pass"),
            ("verdict: approved - looks clean", True, "looks clean"),
            ("Some preamble\nVERDICT: APPROVED\nSome epilogue", True, "Approved"),
            ("Everything looks good, approved!", True, "Reviewer approved changes"),
            ("LGTM, changes look solid", True, "Reviewer approved changes"),
        ]
        for text, exp_approved, exp_reason in cases:
            approved, reason = parse_reviewer_verdict(text)
            self.assertEqual(approved, exp_approved, f"Failed on: {text}")
            if exp_reason:
                self.assertIn(exp_reason.lower(), reason.lower())

    def test_parse_reviewer_verdict_rejected(self):
        cases = [
            ("VERDICT: REJECTED - Missing error handling", False, "Missing error handling"),
            ("VERDICT: FAIL - Syntax error on line 42", False, "Syntax error on line 42"),
            ("verdict: rejected - bug in loop", False, "bug in loop"),
            ("Some preamble\nVERDICT: REJECTED - broken tests\nSome epilogue", False, "broken tests"),
            ("Critical defect: regression introduced in tokenizer", False, "Reviewer identified critical defects"),
            ("", False, "empty response"),
        ]
        for text, exp_approved, exp_reason in cases:
            approved, reason = parse_reviewer_verdict(text)
            self.assertEqual(approved, exp_approved, f"Failed on: {text}")
            if exp_reason:
                self.assertIn(exp_reason.lower(), reason.lower())

    def test_get_workspace_changes_diff_empty(self):
        with mock.patch("core.agent_tools.workspace._ws_changes", {}):
            touched, diff = get_workspace_changes_diff()
            self.assertEqual(touched, [])
            self.assertIn("no workspace files modified", diff)

    def test_get_workspace_changes_diff_detected(self):
        fake_ws = {
            1: {
                "e:/test/sample.py": {
                    "before": "def old():\n    pass\n",
                    "after": "def new():\n    return 42\n",
                }
            }
        }
        with mock.patch("core.agent_tools.workspace._ws_changes", fake_ws), \
             mock.patch("core.request_context.get_current_user_id", return_value=1):
            touched, diff = get_workspace_changes_diff()
            self.assertEqual(touched, ["e:/test/sample.py"])
            self.assertIn("-def old():", diff)
            self.assertIn("+def new():", diff)

    def test_critic_actor_approval_iteration_1(self):
        async def mock_run_subagent(task, role=None, **kwargs):
            if role == "coder":
                return "[sub-agent · role=coder · 2 msgs · lane=executor · status=success]\nCreated auth handler"
            elif role == "reviewer":
                return "[sub-agent · role=reviewer · 2 msgs · lane=main · status=success]\nCode looks great.\nVERDICT: APPROVED"
            return "error: unexpected role"

        with mock.patch("core.subagent.critic.run_subagent", side_effect=mock_run_subagent):
            res = asyncio.run(run_critic_actor_cycle("Implement auth handler", max_iterations=2))
            self.assertEqual(res["status"], "success")
            self.assertEqual(res["iterations"], 1)
            self.assertEqual(res["verdict"], "APPROVED")
            self.assertIn("iterations=1/2", res["output"])
            self.assertIn("verdict=APPROVED", res["output"])
            self.assertTrue(subagent_result_verdict("spawn_reviewed_coder", res["output"]))

    def test_critic_actor_rejection_then_approval_iteration_2(self):
        call_history = []

        async def mock_run_subagent(task, role=None, **kwargs):
            call_history.append((role, task))
            if role == "coder":
                if len(call_history) == 1:
                    return "[sub-agent · role=coder · 2 msgs · lane=executor · status=success]\nInitial attempt"
                else:
                    return "[sub-agent · role=coder · 2 msgs · lane=executor · status=success]\nFixed validation per review"
            elif role == "reviewer":
                if len(call_history) == 2:
                    return "[sub-agent · role=reviewer · 2 msgs · lane=main · status=success]\nMissing input validation.\nVERDICT: REJECTED - Missing input validation"
                else:
                    return "[sub-agent · role=reviewer · 2 msgs · lane=main · status=success]\nValidation added properly.\nVERDICT: APPROVED"
            return "error: unexpected role"

        with mock.patch("core.subagent.critic.run_subagent", side_effect=mock_run_subagent):
            res = asyncio.run(run_critic_actor_cycle("Implement input validation", max_iterations=2))
            self.assertEqual(res["status"], "success")
            self.assertEqual(res["iterations"], 2)
            self.assertEqual(res["verdict"], "APPROVED")
            # Verify coder was re-prompted with critic feedback
            self.assertIn("CRITIC REVIEW FEEDBACK", call_history[2][1])
            self.assertIn("Missing input validation", call_history[2][1])
            self.assertTrue(subagent_result_verdict("spawn_reviewed_coder", res["output"]))

    def test_critic_actor_rejection_exhausts_max_iterations(self):
        async def mock_run_subagent(task, role=None, **kwargs):
            if role == "coder":
                return "[sub-agent · role=coder · 2 msgs · lane=executor · status=success]\nDraft code"
            elif role == "reviewer":
                return "[sub-agent · role=reviewer · 2 msgs · lane=main · status=success]\nStill broken.\nVERDICT: REJECTED - Persistent defect"
            return "error: unexpected role"

        with mock.patch("core.subagent.critic.run_subagent", side_effect=mock_run_subagent):
            res = asyncio.run(run_critic_actor_cycle("Complex task", max_iterations=2))
            self.assertEqual(res["status"], "critic_rejected")
            self.assertEqual(res["iterations"], 2)
            self.assertEqual(res["verdict"], "REJECTED")
            self.assertIn("iterations=2/2", res["output"])
            self.assertIn("verdict=REJECTED", res["output"])
            # Verdict should be False for rejected outcome
            self.assertFalse(subagent_result_verdict("spawn_reviewed_coder", res["output"]))

    def test_critic_actor_empty_task(self):
        res = asyncio.run(run_critic_actor_cycle(""))
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["output"], "error: task is required")

    def test_critic_actor_coder_error(self):
        async def mock_run_subagent(task, role=None, **kwargs):
            return "error: no model is available for this sub-agent task"

        with mock.patch("core.subagent.critic.run_subagent", side_effect=mock_run_subagent):
            res = asyncio.run(run_critic_actor_cycle("Any task"))
            self.assertEqual(res["status"], "error")
            self.assertEqual(res["verdict"], "ERROR")
            self.assertIn("error: no model is available", res["output"])

    def test_critic_actor_pan_masking(self):
        async def mock_run_subagent(task, role=None, **kwargs):
            if role == "coder":
                return "[sub-agent · role=coder · status=success]\nWrote test with card 4111111111111111"
            return "[sub-agent · role=reviewer · status=success]\nApproved 4111111111111111\nVERDICT: APPROVED"

        with mock.patch("core.subagent.critic.run_subagent", side_effect=mock_run_subagent):
            res = asyncio.run(run_critic_actor_cycle("Card processing task"))
            self.assertEqual(res["status"], "success")
            self.assertNotIn("4111111111111111", res["output"])
            self.assertIn("[card ****1111]", res["output"])

    def test_structured_json_review_parsing(self):
        from core.subagent.critic import parse_reviewer_verdict_detailed

        json_review = '''
        Here is my analysis of the diff:
        ```json
        {
          "verdict": "REJECTED",
          "defect_type": "REGRESSION_TEST_FAIL",
          "blocking_issues": ["test_auth_invariant failed on line 42 with KeyError"],
          "patch_suggestion": "Preserve old dictionary key while mapping new schema"
        }
        ```
        '''
        approved, reason, details = parse_reviewer_verdict_detailed(json_review)
        self.assertFalse(approved)
        self.assertEqual(details["defect_type"], "REGRESSION_TEST_FAIL")
        self.assertIn("test_auth_invariant failed", details["blocking_issues"][0])
        self.assertIn("Preserve old dictionary key", details["patch_suggestion"])
        self.assertIn("test_auth_invariant failed", reason)

        json_approved = '''
        ```json
        {
          "verdict": "APPROVED",
          "defect_type": "NONE",
          "blocking_issues": [],
          "patch_suggestion": ""
        }
        ```
        '''
        app, r, det = parse_reviewer_verdict_detailed(json_approved)
        self.assertTrue(app)
        self.assertEqual(det["defect_type"], "NONE")

    def test_critic_actor_syntax_error_telemetry_blocks_approval(self):
        fake_ws = {1: {}}

        async def mock_run_subagent(task, role=None, **kwargs):
            if role == "coder":
                fake_ws[1]["e:/test/broken.py"] = {
                    "before": "def valid(): pass\n",
                    "after": "def broken(\n",  # Syntax error
                }
                return "[sub-agent · role=coder · status=success]\nImplemented feature with broken syntax"
            return "VERDICT: APPROVED"

        with mock.patch("core.subagent.critic.run_subagent", side_effect=mock_run_subagent), \
             mock.patch("core.agent_tools.workspace._ws_changes", fake_ws), \
             mock.patch("core.request_context.get_current_user_id", return_value=1):
            res = asyncio.run(run_critic_actor_cycle("Write broken file", max_iterations=1))
            self.assertEqual(res["status"], "critic_rejected")
            self.assertEqual(res["verdict"], "REJECTED")
            self.assertEqual(res["defect_type"], "SYNTAX_ERROR")
            self.assertTrue(any("broken.py" in str(b) for b in res["blocking_issues"]))


if __name__ == "__main__":
    unittest.main()

