"""Deck / PDF questionnaire: ask once, then build without asking or researching again."""
import unittest

from core import prompt_scope as ps

CARD = ("Before building your deck, a few quick choices.\n```questionnaire\n"
        '{"title":"A few quick choices for your deck","questions":[{"id":"slides","type":"radio","q":"How many slides?",'
        '"options":["5-7","8-12"]}]}\n```')
PDF_CARD = ("```questionnaire\n"
            '{"title":"A few quick choices for your PDF","questions":[{"id":"sections","type":"radio",'
            '"q":"How long?","options":["Short"]}]}\n```')
ANSWERS = "How many slides?: 8-12 (standard)\nWho is it for?: General\nColour palette?: Forest"


class BriefPhase(unittest.TestCase):
    def test_new_request_asks(self):
        self.assertEqual(ps.brief_phase("Make a deep informative PPT of Dhaka with images from web", []), "ask")

    def test_answers_to_the_card_build(self):
        prior = [{"role": "user", "content": "make a ppt of Dhaka"}, {"role": "assistant", "content": CARD}]
        self.assertEqual(ps.brief_phase(ANSWERS, prior), "build")

    def test_new_deck_request_in_a_chat_with_earlier_replies_still_asks(self):
        prior = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "Hello! How can I help?"}]
        self.assertEqual(ps.brief_phase("make a ppt about cyber security", prior), "ask")
        self.assertTrue(ps.deck_brief_wanted("make a ppt about cyber security", prior))

    def test_pdf_flow(self):
        self.assertEqual(ps.brief_phase("create a PDF guide about onboarding", []), "ask")
        prior = [{"role": "assistant", "content": PDF_CARD}]
        self.assertEqual(ps.brief_phase("Short\nTechnical team\nFormal report", prior), "build")

    def test_ordinary_chat_is_not_a_brief(self):
        self.assertEqual(ps.brief_phase("what is the capital of France?", []), "")
        self.assertEqual(ps.brief_phase("thanks", [{"role": "assistant", "content": "Done"}]), "")

    def test_edits_of_an_existing_deck_are_not_a_brief(self):
        self.assertEqual(ps.brief_phase("change the theme of the ppt", []), "")

    def test_the_original_request_is_the_message_before_the_card(self):
        prior = [{"role": "user", "content": "Make a deck about the bank's 2025 results"},
                 {"role": "assistant", "content": CARD}]
        self.assertEqual(ps.brief_request(prior), "Make a deck about the bank's 2025 results")

    def test_the_latest_card_is_the_one_used(self):
        prior = [{"role": "user", "content": "first deck"}, {"role": "assistant", "content": CARD},
                 {"role": "user", "content": ANSWERS}, {"role": "assistant", "content": "Done [DOWNLOAD: a.pptx]"},
                 {"role": "user", "content": "now a PDF about onboarding"}, {"role": "assistant", "content": PDF_CARD}]
        self.assertEqual(ps.brief_request(prior), "now a PDF about onboarding")

    def test_no_card_no_request(self):
        self.assertEqual(ps.brief_request([]), "")
        self.assertEqual(ps.brief_request([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]), "")

    def test_the_chat_route_routes_the_kb_by_it(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / "routes" / "chat" / "run.py").read_text(encoding="utf-8")
        self.assertIn("prompt_scope.brief_request(msgs[:-1])", src)

    def test_build_note_forbids_asking_and_picture_searches(self):
        self.assertIn("Do NOT ask", ps.BRIEF_BUILD_NOW)
        self.assertIn("web_search_images", ps.BRIEF_BUILD_NOW)
        self.assertIn("image:", ps.BRIEF_BUILD_NOW)


if __name__ == "__main__":
    unittest.main()
