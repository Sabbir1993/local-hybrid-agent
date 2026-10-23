"""The web search keeps to the current year unless the user named another.

Run: python -m unittest tests.test_web_year -v
"""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import web_tools
from core.request_context import set_user_text
from core.web_search.rerank import detect_recency
from core.web_tools import freshen_years

ASKED = "Is there any vulnerability found regarding any programming language?"


class FreshenYearTests(unittest.TestCase):
    def test_a_past_year_becomes_this_year_when_the_user_named_none(self):
        self.assertEqual(freshen_years("language vulnerabilities 2024 2025 security issues", ASKED, 2026),
                         "language vulnerabilities 2026 security issues")

    def test_a_year_the_user_asked_for_is_kept(self):
        self.assertEqual(freshen_years("vulnerabilities 2024", "what happened in 2024?", 2026), "vulnerabilities 2024")

    def test_ids_versions_and_history_are_left_alone(self):
        self.assertEqual(freshen_years("CVE-2024-3094 xz backdoor", ASKED, 2026), "CVE-2024-3094 xz backdoor")
        self.assertEqual(freshen_years("python 2024.1 release", ASKED, 2026), "python 2024.1 release")
        self.assertEqual(freshen_years("history of 2022 breaches", ASKED, 2026), "history of 2022 breaches")

    def test_no_user_text_known_still_never_touches_future_or_old_years(self):
        self.assertEqual(freshen_years("plan for 2030 and 2001", "", 2026), "plan for 2030 and 2001")


class SearchUsesItTests(unittest.TestCase):
    def test_the_engine_gets_the_updated_year_and_a_note_is_shown(self):
        seen = {}

        async def fake_search(query, recency="", extra_queries=None):
            seen.update(query=query)
            return {"query": query, "engines": ["duckduckgo"], "errors": {},
                    "results": [{"title": "t", "url": "https://example.org/a", "snippet": "s"}]}
        set_user_text(ASKED)
        with mock.patch.object(web_tools.ws, "search", fake_search), mock.patch.object(web_tools.citations, "remember"):
            out = asyncio.run(web_tools.tool_web_search({"query": "programming language vulnerabilities 2025 critical CVE"}))
        self.assertIn("2026", seen["query"])
        self.assertNotIn("2025", seen["query"])
        self.assertIn("out-of-date year in the query was changed", out)


class RecencyTests(unittest.TestCase):
    def test_security_news_defaults_to_the_past_year(self):
        self.assertEqual(detect_recency("programming language vulnerabilities critical security issues"), "year")

    def test_a_specific_cve_or_evergreen_topic_is_not_limited(self):
        self.assertEqual(detect_recency("CVE-2021-44228 log4shell vulnerability"), "")
        self.assertEqual(detect_recency("what is a python decorator"), "")


if __name__ == "__main__":
    unittest.main()
