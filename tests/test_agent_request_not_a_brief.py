import unittest

from core import prompt_scope


class AgentBuildIsNotADocumentBrief(unittest.TestCase):
    def test_a_request_to_build_an_agent_skips_the_deck_questionnaire(self):
        q = ("I want to make a custom agent that can analysis and generate ppt/html/pdf by deep analysing given data "
             "source. for this shared input fields from attached image")
        self.assertEqual(prompt_scope.brief_phase(q), "")

    def test_a_request_for_a_prompt_skips_the_questionnaire(self):
        for q in ("I want a prompt that can analysis and generate what user asks by deep analysing given data source",
                  "write a prompt that generates a pdf report from the data source"):
            self.assertEqual(prompt_scope.brief_phase(q), "", q)

    def test_a_real_deck_request_still_asks_first(self):
        self.assertEqual(prompt_scope.brief_phase("make a 10 slide ppt about BFSI sales for executives"), "ask")


if __name__ == "__main__":
    unittest.main()
