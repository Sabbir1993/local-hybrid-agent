"""Questionnaire repair (core/questionnaire.py), download-tag verification and inline-HTML saving (routes/chat/delivery.py)."""
import json
import unittest
from unittest import mock

from core import output_guard, questionnaire
from routes.chat import delivery

HTML = "<!DOCTYPE html>\n<html><head><title>Village</title></head><body>" + "<p>village</p>" * 60 + "</body></html>"


class QuestionnaireTests(unittest.TestCase):
    def test_repairs_common_model_slips(self):
        bad = ('{"title":“Hi”,"questions":[{"id":"a","type":"radio","q":"Budget?\n(Tk)",'
               '"options":["A","B",]},],}')
        out, changed = questionnaire.repair("Intro\n```questionnaire\n" + bad + "\n```")
        self.assertTrue(changed)
        body = out.split("```questionnaire\n")[1].split("\n```")[0]
        data = json.loads(body)
        self.assertEqual(data["questions"][0]["options"], ["A", "B"])

    def test_valid_block_is_untouched(self):
        text = '```questionnaire\n{"title":"t","questions":[{"id":"a","type":"text","q":"x"}]}\n```'
        self.assertEqual(questionnaire.repair(text), (text, False))

    def test_hopeless_block_is_left_and_logged(self):
        with self.assertLogs("questionnaire", level="WARNING"):
            self.assertFalse(questionnaire.repair("```questionnaire\nnot json at all\n```")[1])


class RedactorResetTests(unittest.TestCase):
    def test_reset_clears_fence_state(self):
        red = output_guard.OutputRedactor(None, False, fx=True)
        red.feed("```questionnaire\n{")
        red.reset()
        self.assertFalse(red.in_fence)


class DownloadTagTests(unittest.TestCase):
    def test_tag_without_file_is_dropped_with_note(self):
        out, changed = delivery.verify_download_tags("Here: [DOWNLOAD: ghost.html] done", [], [], lambda n: False)
        self.assertTrue(changed)
        self.assertNotIn("[DOWNLOAD", out)
        self.assertIn("No file was created", out)

    def test_wrong_name_points_at_the_one_written_file(self):
        out, _ = delivery.verify_download_tags("[DOWNLOAD: made-up-name.html]", ["village-ab12.html"], [], lambda n: False)
        self.assertIn("[DOWNLOAD: village-ab12.html]", out)

    def test_real_file_tag_kept(self):
        text = "[DOWNLOAD: a.html]"
        self.assertEqual(delivery.verify_download_tags(text, [], ["a.html"], lambda n: True), (text, False))
        self.assertEqual(delivery.verify_download_tags(text, ["a.html"], [], lambda n: False), (text, False))

    def test_name_known_from_an_earlier_reply_but_never_saved_is_dropped(self):
        out, changed = delivery.verify_download_tags("[DOWNLOAD: graph.html]", [], ["graph.html"], lambda n: False)
        self.assertTrue(changed)
        self.assertNotIn("[DOWNLOAD:", out)
        self.assertIn("No file was created for `graph.html`", out)

    def test_page_without_doctype_is_found_and_a_cut_off_page_is_not(self):
        page = "<html><head><title>G</title></head><body>" + "x" * 500 + "</body></html>"
        found = delivery.inline_html_deliverable("[DOWNLOAD: graph.html]\n```html\n" + page + "\n```", "make graph.html")
        self.assertEqual(found, ("graph.html", page))
        cut = "```html\n<html><body>" + "x" * 500 + "\n```"
        self.assertIsNone(delivery.inline_html_deliverable("[DOWNLOAD: graph.html]\n" + cut, "make graph.html"))

    def test_inline_html_is_saved_and_linked(self):
        reply = ("Save the following as `village-3d.html`.\n[DOWNLOAD: village-3d-homes.html]\n```html\n" + HTML + "\n```")
        saved = {}

        def fake_write(args):
            saved.update(args)
            return "ok [DOWNLOAD: village-3d-9f.html]"
        written: list = []
        with mock.patch.object(delivery, "chat_write_file", fake_write):
            out, changed = delivery.finalize_reply_files(reply, written, "make a 3D village html", [], True)
        self.assertTrue(changed)
        self.assertEqual(written, ["village-3d-9f.html"])
        self.assertEqual(saved["path"], "village-3d.html")
        self.assertIn("[DOWNLOAD: village-3d-9f.html]", out)
        self.assertNotIn("village-3d-homes.html", out)

    def test_no_write_permission_drops_dead_tag(self):
        out, _ = delivery.finalize_reply_files("[DOWNLOAD: x.html]\n```html\n" + HTML + "\n```", [], "html page", [], False)
        self.assertNotIn("[DOWNLOAD: x.html]", out)


if __name__ == "__main__":
    unittest.main()
