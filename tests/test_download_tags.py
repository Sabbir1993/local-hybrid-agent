"""A download tag the model cut off must never swallow the real one (it hid the file and made "where is the html?" answer with nothing).

Run: python -m unittest tests.test_download_tags -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import prompt_scope
from core.agent_tools.project_files import SINGLE_PAGE_MANUAL
from routes.chat.delivery import _finalize_download_tags
from routes.chat.file_intent import _DL_TAG_RE, session_files

CUT = "Done.\n\n[DOWNLOAD: vuln-dashboard-d15b\n\n[DOWNLOAD: vuln-dashboard-d15b71c3.html]"


class CutOffTagTests(unittest.TestCase):
    def test_only_the_real_tag_is_found(self):
        self.assertEqual(_DL_TAG_RE.findall(CUT), ["vuln-dashboard-d15b71c3.html"])

    def test_the_cut_tag_is_removed_and_the_real_one_kept_once(self):
        out, changed = _finalize_download_tags(CUT, ["vuln-dashboard-d15b71c3.html"], "make a detailed html")
        self.assertTrue(changed)
        self.assertEqual(out.count("[DOWNLOAD:"), 1)
        self.assertNotIn("d15b\n", out)
        self.assertTrue(out.endswith("[DOWNLOAD: vuln-dashboard-d15b71c3.html]"))

    def test_a_reply_with_no_tag_gets_one(self):
        out, _ = _finalize_download_tags("All done.", ["page-1234abcd.html"], "make an html")
        self.assertEqual(out, "All done.\n\n[DOWNLOAD: page-1234abcd.html]")

    def test_the_chat_remembers_the_real_file_for_where_is_it(self):
        msgs = [{"role": "user", "content": "make html"}, {"role": "assistant", "content": CUT}]
        self.assertEqual(session_files(msgs), ["vuln-dashboard-d15b71c3.html"])


class MisspelledTagTests(unittest.TestCase):
    REAL = "lang-vuln-report-6cb4a5dc.html"

    def test_a_near_miss_of_the_real_name_is_dropped_and_the_real_link_kept_once(self):
        c = "Done.\n\n[DOWNLOAD: lang-vuln-report-6cb.4a5dc.html]\n\n[DOWNLOAD: " + self.REAL + "]"
        out, _ = _finalize_download_tags(c, [self.REAL], "make an html")
        self.assertEqual(out.count("[DOWNLOAD:"), 1)
        self.assertIn(self.REAL, out)

    def test_only_the_misspelled_tag_present_is_replaced_by_the_real_one(self):
        out, _ = _finalize_download_tags("Done.\n\n[DOWNLOAD: lang-vuln-report-6cb.4a5dc.html]", [self.REAL], "make an html")
        self.assertEqual(out, "Done.\n\n[DOWNLOAD: " + self.REAL + "]")

    def test_an_unrelated_file_the_reply_mentions_is_left_alone(self):
        out, _ = _finalize_download_tags("See [DOWNLOAD: other-file.html]", [self.REAL], "make an html")
        self.assertIn("[DOWNLOAD: other-file.html]", out)


class PageKindTests(unittest.TestCase):
    def test_a_report_is_not_turned_into_a_dashboard(self):
        for manual in (SINGLE_PAGE_MANUAL, prompt_scope.HTML_MANUAL):
            self.assertIn("MATCH THE KIND OF PAGE", manual)
            self.assertIn("only when the user said dashboard", manual)
        self.assertNotIn("WEB DASHBOARDS", prompt_scope.HTML_MANUAL)


if __name__ == "__main__":
    unittest.main()
