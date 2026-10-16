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


class ServerCard(unittest.TestCase):
    """The deck / PDF questionnaire is written by the server: it must parse, and its answers must continue into the build phase."""

    def card(self, kind):
        import json
        text = ps.brief_card(kind)
        body = text.split("```questionnaire\n")[1].split("\n```")[0]
        return text, json.loads(body)

    def test_the_deck_card_has_the_questions_the_brief_expects(self):
        text, card = self.card("deck")
        self.assertEqual([q["id"] for q in card["questions"]], ["slides", "audience", "palette", "style", "extra"])
        self.assertEqual(len(card["questions"][2]["options"]), 8)
        self.assertEqual(card["submit_label"], "Build my deck")
        self.assertTrue(text.startswith("Before building your deck"))

    def test_the_pdf_card(self):
        text, card = self.card("pdf")
        self.assertEqual([q["id"] for q in card["questions"]], ["sections", "audience", "tone", "extra"])
        self.assertEqual(card["submit_label"], "Build my PDF")

    def test_the_answers_to_the_server_card_move_to_the_build_phase(self):
        deck, _ = self.card("deck")
        prior = [{"role": "user", "content": "make a ppt of Dhaka"}, {"role": "assistant", "content": deck}]
        self.assertEqual(ps.brief_phase(ANSWERS, prior), "build")
        self.assertEqual(ps.brief_request(prior), "make a ppt of Dhaka")
        pdf, _ = self.card("pdf")
        self.assertEqual(ps.brief_phase("Short\nTechnical team\nFormal report", [{"role": "assistant", "content": pdf}]), "build")

    def test_the_model_writes_the_card_the_server_copy_is_only_a_fallback(self):
        from pathlib import Path
        self.assertEqual(ps.brief_phase("Make a deep informative PPT of Dhaka with its visual image and graph", []), "ask")
        src = (Path(__file__).resolve().parents[1] / "routes" / "chat" / "run.py").read_text(encoding="utf-8")
        self.assertIn('"brief_card_server", "fallback"', src)
        self.assertIn('card_mode == "always"', src)                                 # the always-server mode is opt-in
        self.assertEqual(src.count('"```questionnaire" not in content'), 2)        # fallback at both final-answer paths
        self.assertIn("DECK_BRIEF", ps.DECK_BRIEF.replace("POWERPOINT REQUEST", "DECK_BRIEF"))
        self.assertIn("```questionnaire", ps.DECK_BRIEF)                            # the model is still told to write the card


class BriefCardRecoveryTests(unittest.TestCase):
    """The questionnaire card is the reply to a PDF/deck request; file-intent recovery must not save its JSON as document.pdf."""

    CARD = 'A few quick choices:\n```questionnaire\n{"title": "PDF brief", "questions": []}\n```'

    def test_card_reply_is_served_not_saved(self):
        from routes.chat.file_intent import serves_brief_card
        self.assertTrue(serves_brief_card(self.CARD, ""))
        self.assertTrue(serves_brief_card("no card here", "ask"))      # the server's brief_card fallback fills it in
        self.assertFalse(serves_brief_card("| a | b |\n|---|---|\n| 1 | 2 |", "build"))
        self.assertFalse(serves_brief_card("```json\n{}\n```", ""))

    def test_kb_tools_are_known_names(self):
        from routes.chat.file_intent import KB_TOOL_NAMES
        self.assertIn("search_knowledge_base", KB_TOOL_NAMES)
