"""A follow-up about a deck that is already in the chat continues THAT deck, and the last turn of a build keeps its document tools."""
import unittest
from pathlib import Path

import core.agent_tools  # noqa: F401

from routes.chat import file_intent as fi

DECK = ["Dhaka_Deck-1849c4ae.pptx"]


class DeckFollowUp(unittest.TestCase):
    def test_content_talk_after_a_deck_is_an_edit_of_it(self):
        for q in ("where is image, graphs  & infographics ?", "add a chart of population growth", "the slides have no pictures",
                  "change the colour theme", "make the cover better", "why are there no infographics"):
            self.assertEqual(fi.file_followup_intent(q, True, DECK), "edit", q)

    def test_a_request_for_a_new_deck_or_unrelated_talk_is_not(self):
        for q in ("make a new presentation about Chattogram", "create another deck for the board", "what is the capital of France?",
                  "thanks"):
            self.assertNotEqual(fi.file_followup_intent(q, True, DECK), "edit", q)

    def test_without_a_deck_nothing_changes(self):
        self.assertIsNone(fi.file_followup_intent("where is image, graphs & infographics ?", True, ["notes.md"]))

    def test_where_is_the_file_still_means_send_the_file(self):
        self.assertEqual(fi.file_followup_intent("where is the file", True, DECK), "where")


class FinalTurn(unittest.TestCase):
    def test_the_last_turn_of_a_document_build_keeps_the_document_tools(self):
        src = (Path(__file__).resolve().parents[1] / "routes" / "chat" / "run.py").read_text(encoding="utf-8")
        self.assertIn('doc_names = ("doc_create", "doc_append", "doc_inspect", "doc_edit")', src)
        self.assertIn('(brief_phase == "build" or resume_note or longdoc_build) and wants_file', src)
        self.assertIn("current_tools = doc_tools", src)


class RealDatabaseIsLeftAlone(unittest.TestCase):
    def test_the_suite_does_not_write_decks_into_the_real_projects_db(self):
        import os
        from core.config import PROJECTS_DB_FILE
        from core.db import _projects_db
        from core.doc_tools import builds
        from unittest import mock
        self.assertEqual(getattr(_projects_db, "path", ""), str(PROJECTS_DB_FILE))
        with mock.patch.dict(os.environ, {"A770_NO_USAGE_WRITES": "1"}):         # what tests/test_00_isolation.py sets for the whole run
            before = _projects_db.execute("SELECT COUNT(*) FROM deck_builds").fetchone()[0]
            builds.save(5, "isolation-check.pptx", total=3, have=1)
            builds.close(5, "isolation-check.pptx")
            self.assertEqual(_projects_db.execute("SELECT COUNT(*) FROM deck_builds").fetchone()[0], before)


if __name__ == "__main__":
    unittest.main()
