"""Advice-style chat answers: when the style block is added and when research-style questions search the web."""
import unittest

from core import prompt_scope as ps

CAT = "my Persian cat is skinny, any supplement to boost its health?"


class ChatStyle(unittest.TestCase):
    def test_advice_questions_get_the_style_block(self):
        self.assertTrue(ps.style_wanted(CAT))
        self.assertTrue(ps.style_wanted("which laptop is best for a student on a budget?"))

    def test_small_talk_code_files_and_voice_do_not(self):
        for q in ("hi", "thanks!", "write a python function to sort lists please", "summarise this file for me please",
                  "```js\nconsole.log(1)\n``` how can I fix this"):
            self.assertFalse(ps.style_wanted(q), q)
        self.assertFalse(ps.style_wanted(CAT, voice=True))
        self.assertFalse(ps.style_wanted(CAT, enabled=False))

    def test_follow_ups_stay_in_style_after_a_questionnaire(self):
        prior = [{"role": "assistant", "content": "Plan...\n```questionnaire\n{}\n```"}]
        self.assertTrue(ps.style_wanted("Q: How old is it?: 1-7 years", prior))
        self.assertFalse(ps.style_wanted("Q: How old is it?: 1-7 years", []))

    def test_research_web_is_off_unless_asked_for(self):
        self.assertFalse(ps.web_intent(CAT))
        self.assertTrue(ps.web_intent(CAT, research=True))
        self.assertFalse(ps.web_intent("write a python function that is best for sorting", research=True))

    def test_block_mentions_the_questionnaire_contract(self):
        self.assertIn("```questionnaire", ps.CHAT_STYLE)
        self.assertIn("radio | checkbox | text", ps.CHAT_STYLE)


if __name__ == "__main__":
    unittest.main()
