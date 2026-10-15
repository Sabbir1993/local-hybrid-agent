"""Deck / PDF builds in pieces survive a restart; a bare "continue" resumes them."""
import unittest
import uuid

import core.agent_tools  # noqa: F401  (import order: the doc tools need the agent tools loaded first)
from core.doc_tools import builds, operations
from core.db import _projects_db

UID = 987001


def fk():
    return "t-" + uuid.uuid4().hex[:10] + ".pptx"


class Store(unittest.TestCase):
    def tearDown(self):
        _projects_db.execute("DELETE FROM deck_builds WHERE user_id = ?", (UID,))
        _projects_db.commit()

    def test_save_load_update_close(self):
        f = fk()
        builds.save(UID, f, total=10, plan=[("Intro", "hook"), ("Facts", "")], have=2)
        b = builds.load(UID, f)
        self.assertEqual((b["total"], b["have"]), (10, 2))
        self.assertEqual(b["plan"][0][0], "Intro")
        builds.save(UID, f, have=5)                       # None fields keep their value
        b = builds.load(UID, f)
        self.assertEqual((b["total"], b["have"], len(b["plan"])), (10, 5, 2))
        builds.close(UID, f)
        self.assertIsNone(builds.load(UID, f))

    def test_expired_builds_are_ignored(self):
        f = fk()
        builds.save(UID, f, total=4, have=1)
        _projects_db.execute("UPDATE deck_builds SET updated_at = 1 WHERE user_id = ?", (UID,))
        _projects_db.commit()
        self.assertIsNone(builds.load(UID, f))
        self.assertIsNone(builds.latest_open(UID))

    def test_the_cache_is_rebuilt_from_the_store_after_a_restart(self):
        f = fk()
        builds.save(UID, f, total=8, plan=[("One", "a"), ("Two", "b"), ("Three", "c")], have=1)
        operations._BUILD_PLAN.clear()
        operations._BUILD_TOTAL.clear()                   # what a restart does
        key = (UID, f)
        self.assertEqual(operations._total_for(key), 8)
        self.assertIn("Next in your plan: 2. Two", operations._plan_note(key, 1, ["One"], False))


class Resume(unittest.TestCase):
    def tearDown(self):
        _projects_db.execute("DELETE FROM deck_builds WHERE user_id = ?", (UID,))
        _projects_db.commit()

    def test_intent(self):
        for t in ("continue", "Continue.", "ok go on", "please keep going", "finish the deck", "resume"):
            self.assertTrue(builds.is_continue(t), t)
        for t in ("continue the story of Dhaka in detail with new facts about the 1971 war and its aftermath in depth",
                  "what is a deck", "make a ppt about cats"):
            self.assertFalse(builds.is_continue(t), t)

    def test_note_for_an_open_build(self):
        f = fk()
        builds.save(UID, f, total=6, plan=[("A", ""), ("B", ""), ("C", ""), ("D", "")], have=2)
        note = builds.resume_note(UID, "continue")
        self.assertIn(f, note)
        self.assertIn("2 of 6", note)
        self.assertIn("3. C", note)
        self.assertIn("Do NOT restart", note)

    def test_no_note_when_nothing_is_open_or_message_is_other(self):
        self.assertEqual(builds.resume_note(UID, "continue"), "")
        builds.save(UID, fk(), total=6, have=2)
        self.assertEqual(builds.resume_note(UID, "tell me a joke"), "")
        self.assertEqual(builds.resume_note(UID + 1, "continue"), "")


if __name__ == "__main__":
    unittest.main()


class ForcedMode(unittest.TestCase):
    def run_prepare(self, mode, content):
        import asyncio
        tok = operations.forced_mode.set(mode)
        try:
            return asyncio.run(operations.prepare_build(".pptx", content, None))[0]
        finally:
            operations.forced_mode.reset(tok)

    def test_the_answer_adds_designer_when_the_model_forgot(self):
        out = self.run_prepare("designer", "theme: sunset\n# Dhaka\n---\n## A\n- b\n")
        self.assertIn("mode: designer", out)

    def test_an_existing_mode_line_is_not_doubled(self):
        out = self.run_prepare("designer", "theme: sunset\nmode: designer\n# Dhaka\n")
        self.assertEqual(out.lower().count("mode: designer"), 1)

    def test_editable_answer_removes_it(self):
        self.assertNotIn("mode: designer", self.run_prepare("editable", "theme: sunset\nmode: designer\n# Dhaka\n"))

    def test_no_answer_changes_nothing(self):
        self.assertNotIn("mode: designer", self.run_prepare("", "theme: sunset\n# Dhaka\n"))
