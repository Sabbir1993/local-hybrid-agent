"""web_fetch refuses placeholder addresses before any network work, and diagram guidance steers away from ASCII art."""
import asyncio
import unittest
from unittest import mock

from core import prompt_scope, web_tools


class PlaceholderUrlTests(unittest.TestCase):
    def fetch(self, url):
        with mock.patch.object(web_tools.ws, "fetch_page", side_effect=AssertionError("network used")) as fp:
            out = asyncio.run(web_tools.tool_web_fetch({"url": url}))
        return out, fp.called

    def test_placeholders_are_refused_without_fetching(self):
        for url in ("https://...", "https://example", "...", "https://localhost/x"):
            out, fetched = self.fetch(url)
            self.assertIn("is not a real address", out, url)
            self.assertFalse(fetched, url)

    def test_real_hosts_pass_the_check(self):
        for host in ("example.com", "www.bbc.co.uk", "8.8.8.8", "::1"):
            self.assertFalse(web_tools._placeholder_host(host), host)


class AttachedDataWebTests(unittest.TestCase):
    """A URL or a word like "page" inside an attached sheet must not start a web fetch or switch the web on."""
    MSG = ("make a knowledge graph html from these files\n--- FILE: rec.xlsx ---\n| Link | Evidence |\n"
           "| https://… | review page |\n[Spreadsheet profile above ... data_query(file=\"rec.xlsx\")]\n--- END rec.xlsx ---")

    def test_links_inside_attachments_are_ignored(self):
        from core.knowledge_router import own_words
        own = own_words(self.MSG, 2000)
        self.assertNotIn("https://", own)
        self.assertEqual(prompt_scope.typed_urls(own), [])

    def test_links_the_user_typed_are_kept_but_cut_off_ones_are_not(self):
        self.assertEqual(prompt_scope.typed_urls("summarise https://example.com/a please"), ["https://example.com/a"])
        self.assertEqual(prompt_scope.typed_urls("see https://… and https://..."), [])

    def test_web_tools_with_attached_data_only_when_asked(self):
        self.assertFalse(prompt_scope.web_allowed_with_data(True, False, False))   # globe on, not asked
        self.assertTrue(prompt_scope.web_allowed_with_data(True, True, False))     # user asked for outside facts
        self.assertTrue(prompt_scope.web_allowed_with_data(True, False, True))     # Deep research
        self.assertTrue(prompt_scope.web_allowed_with_data(False, False, False))   # no attached data: unchanged


class DiagramGuidanceTests(unittest.TestCase):
    def test_asking_for_mermaid_keeps_the_visual_guidance(self):
        self.assertTrue(prompt_scope.visual_wanted("draw a mermaid diagram of the login flow"))

    def test_guidance_allows_mermaid_and_forbids_box_characters(self):
        self.assertIn("fenced `mermaid` block", prompt_scope.VISUAL_STYLE)
        self.assertIn("box-drawing", prompt_scope.NO_ASCII_DIAGRAMS)
        self.assertIn("mermaid", prompt_scope.NO_ASCII_DIAGRAMS)


if __name__ == "__main__":
    unittest.main()
