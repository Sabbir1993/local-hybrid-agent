"""Tests to ensure edits and modifications of existing presentations and PDFs never re-trigger questionnaires."""
import unittest

from core import prompt_scope as ps
from routes.chat import file_intent as fi


DECK_FILES = ["dhaka_presentation-4873f548.pptx"]
PDF_FILES = ["annual_report-9812a1f2.pdf"]


class EditFollowupNoBriefTests(unittest.TestCase):
    def test_user_exact_query_is_edit_not_brief(self):
        q = "now make it editable and add a new deck about population growth [chart] after 10 no deck."
        # Must be recognized as an edit
        followup = fi.file_followup_intent(q, True, DECK_FILES)
        self.assertEqual(followup, "edit", "The user's query must be classified as an edit")

        # Must NOT trigger deck brief or questionnaire
        self.assertFalse(ps.deck_brief_wanted(q, [], DECK_FILES))
        self.assertEqual(ps.brief_phase(q, [], DECK_FILES), "")

    def test_various_ppt_edit_variations(self):
        for q in (
            "make it editable",
            "add a new slide about economy after slide 5",
            "add a chart slide after 3",
            "insert a slide about tourism",
            "change the cover theme to ember",
            "make it 12 slides",
            "restyle it in corporate theme",
        ):
            self.assertEqual(fi.file_followup_intent(q, True, DECK_FILES), "edit", f"Failed on query: {q}")
            self.assertFalse(ps.deck_brief_wanted(q, [], DECK_FILES), f"Deck brief wanted on edit query: {q}")
            self.assertEqual(ps.brief_phase(q, [], DECK_FILES), "", f"Brief phase must be empty on edit query: {q}")

    def test_pdf_edit_variations(self):
        for q in (
            "add a section about methodology",
            "expand section 2 with a table",
            "make it 8 sections",
            "add a new page with a comparison chart",
            "update the conclusion paragraph",
        ):
            self.assertEqual(fi.file_followup_intent(q, True, PDF_FILES), "edit", f"Failed on query: {q}")
            self.assertFalse(ps.pdf_brief_wanted(q, [], PDF_FILES), f"PDF brief wanted on edit query: {q}")
            self.assertEqual(ps.brief_phase(q, [], PDF_FILES), "", f"Brief phase must be empty on edit query: {q}")

    def test_explicit_brand_new_topic_still_asks_brief(self):
        # A completely new presentation on a distinct topic with no reference to existing
        q = "create a new presentation about Quantum Computing"
        self.assertNotEqual(fi.file_followup_intent(q, True, DECK_FILES), "edit")
        self.assertTrue(ps.deck_brief_wanted(q, [], DECK_FILES))
        self.assertEqual(ps.brief_phase(q, [], DECK_FILES), "ask")


if __name__ == "__main__":
    unittest.main()
