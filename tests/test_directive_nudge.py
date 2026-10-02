"""tests/test_directive_nudge.py - Directive Action Nudging on Step-1 Passivity.

Verifies that:
1. is_passive_refusal correctly identifies passive model deferrals when creation is requested.
2. It does not false-positive on informational tasks or after successful tool mutations.
3. Expanded refusal phrases (e.g. asking for file paths, starter code) trigger properly.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import unittest
from core.agent_loop.sandbox import is_passive_refusal, PASSIVE_REFUSAL_NOTE, validate_and_finalize_response


class TestDirectiveNudge(unittest.TestCase):
    def test_passive_refusal_detected(self):
        query = "Create a modern landing page for a coffee shop"
        bad_answers = [
            "Please specify the exact file path where you want me to save this.",
            "I can build this! Please provide the code or HTML you want me to start with.",
            "Sure, what content would you like me to include? Please specify the file path.",
            "Where should I save the new file? Please provide the exact file.",
        ]
        for ans in bad_answers:
            self.assertTrue(
                is_passive_refusal(ans, query, []),
                f"Expected passive refusal for: {ans}"
            )
            # Also verify validate_and_finalize_response returns PASSIVE_REFUSAL_NOTE
            text, was_synth, note = validate_and_finalize_response(query, ans, "", [])
            self.assertEqual(note, PASSIVE_REFUSAL_NOTE)

    def test_no_false_positive_when_tool_already_modified(self):
        query = "Create a modern landing page for a coffee shop"
        ans = "Please specify the exact file path if you want additional changes."
        # If write_file was already called and succeeded:
        actions = [{"name": "write_file", "ok": True, "args": {"path": "index.html"}}]
        self.assertFalse(is_passive_refusal(ans, query, actions))

    def test_no_false_positive_on_info_queries(self):
        query = "How do I configure Vulkan on dual Intel Arc A770?"
        ans = "Please specify the exact file path to your config if you want me to check it."
        # Informational query, not creation request
        self.assertFalse(is_passive_refusal(ans, query, []))

    def test_normal_action_or_explanation_is_not_refusal(self):
        query = "Create a python script to test GPU speed"
        good_ans = "I will write the test script in `benchmark.py` and run it."
        self.assertFalse(is_passive_refusal(good_ans, query, []))


if __name__ == "__main__":
    unittest.main()
