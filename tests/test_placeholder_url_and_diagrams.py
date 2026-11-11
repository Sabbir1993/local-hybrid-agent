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


class DiagramGuidanceTests(unittest.TestCase):
    def test_asking_for_mermaid_keeps_the_visual_guidance(self):
        self.assertTrue(prompt_scope.visual_wanted("draw a mermaid diagram of the login flow"))

    def test_guidance_allows_mermaid_and_forbids_box_characters(self):
        self.assertIn("fenced `mermaid` block", prompt_scope.VISUAL_STYLE)
        self.assertIn("box-drawing", prompt_scope.NO_ASCII_DIAGRAMS)
        self.assertIn("mermaid", prompt_scope.NO_ASCII_DIAGRAMS)


if __name__ == "__main__":
    unittest.main()
