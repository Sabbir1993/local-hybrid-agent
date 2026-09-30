import unittest

from core import router_policy as rp

ACTION_Q = "can you test this app on my browser ? and check is there any issues"


def _esc(content, q=ACTION_Q, step=0, calls=()):
    return rp.escalate_reason(step=step, content=content, tool_calls=list(calls), query=q, is_loop=False)


class EscalateOnWhatTheTurnDidTests(unittest.TestCase):
    def test_action_request_without_a_tool_call_escalates_whatever_the_text_says(self):
        for text in ("[Let me start by reading the skill.]", "x" * 900, "Sure! First I would open the app."):
            self.assertEqual(_esc(text), "no_tool_call", text[:30])

    def test_empty_and_code_dump_keep_their_own_reasons(self):
        self.assertEqual(_esc(""), "empty")
        self.assertEqual(_esc("```py\nprint(1)\n```"), "tutorial_code")

    def test_tool_call_or_later_step_never_escalates_on_this_rule(self):
        self.assertEqual(_esc("", calls=[{"x": 1}]), "")
        self.assertEqual(_esc("all done", step=2), "")

    def test_knowledge_question_with_an_action_word_is_answered_by_the_executor(self):
        self.assertEqual(_esc("Use open(path).read().", q="how do I read a file in python?"), "")

    def test_a_run_that_escalated_for_not_acting_stays_on_main(self):
        for reason in ("no_tool_call", "empty", "creation_no_tool", "refused", "tutorial_code"):
            self.assertIn(reason, rp.SUSTAIN_REASONS)
        self.assertNotIn("loop", rp.SUSTAIN_REASONS)     # a transient degenerate reply keeps its retry

    def test_loop_always_escalates(self):
        self.assertEqual(rp.escalate_reason(step=3, content="x", tool_calls=[{"x": 1}], query="hi",
                                            is_loop=True), "loop")


if __name__ == "__main__":
    unittest.main()
