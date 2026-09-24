"""tests/test_chat_loop.py - chat loop guards: current date, KB relevance gate,
deliverable detection, and context trimming for long research loops.

Run: python -m unittest tests.test_chat_loop -v
"""

import asyncio
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import memory, knowledge_router
from routes import common
from routes.chat import (wants_file_output, looks_undelivered, shrink_old_tool_results,
                         session_files, file_followup_intent)

MARKET_Q = ("hi can you prepare of a market analysis html based on web based online gaming "
            "market size? local [BD] along with foreign ?")


class DatePromptTests(unittest.TestCase):
    def test_contains_today_and_year(self):
        now = datetime.now().astimezone()
        line = common.current_date_prompt()
        self.assertIn(now.strftime("%Y-%m-%d"), line)
        self.assertIn(str(now.year), line)
        self.assertIn(str(now.year - 1), line)


class DeliverableTests(unittest.TestCase):
    def test_wants_file(self):
        self.assertTrue(wants_file_output(MARKET_Q))
        self.assertTrue(wants_file_output("create an HTML dashboard of sales"))
        self.assertTrue(wants_file_output("export this to csv"))
        self.assertFalse(wants_file_output("what is the capital of Bangladesh?"))
        self.assertFalse(wants_file_output("what's wrong in this code"))

    def test_announced_preamble_is_undelivered(self):
        pre = ("The web search isn't returning detailed reports, but I have reliable data. "
               "Let me compile the comprehensive HTML market analysis for you now:")
        self.assertTrue(looks_undelivered(pre, wants_file=True, delivered=False))
        self.assertTrue(looks_undelivered(pre, wants_file=False, delivered=False))

    def test_delivered_or_plain_answers_pass(self):
        self.assertFalse(looks_undelivered("x", wants_file=True, delivered=True))
        self.assertFalse(looks_undelivered("```html\n<html></html>\n```", wants_file=True, delivered=False))
        self.assertFalse(looks_undelivered("Dhaka is the capital.", wants_file=False, delivered=False))


    def test_finalize_announcement_is_undelivered(self):
        pre = ("I'll finalize everything into an actual downloadable file right now - let me generate "
               "the complete, fully-polished version using write_file so you can grab it directly.")
        self.assertTrue(looks_undelivered(pre, wants_file=False, delivered=False))


class FileFollowupTests(unittest.TestCase):
    HISTORY = [
        {"role": "user", "content": MARKET_Q},
        {"role": "assistant", "content": "Done.\n\n[DOWNLOAD: BD_Report-1d5aafc8.html]"},
        {"role": "user", "content": "make it nicer"},
        {"role": "assistant", "content": "Updated.\n\n[DOWNLOAD: BD_Report-419c2cb8.html]"},
    ]

    def test_session_files_latest_last(self):
        self.assertEqual(session_files(self.HISTORY), ["BD_Report-1d5aafc8.html", "BD_Report-419c2cb8.html"])
        self.assertEqual(session_files([{"role": "user", "content": "[DOWNLOAD: x.html]"}]), [])

    def test_where_intent(self):
        for q in ("where is file ?", "file not shared", "give me the download link", "share the report again"):
            self.assertEqual(file_followup_intent(q, True), "where", q)
        self.assertIsNone(file_followup_intent("where is file ?", False))

    def test_edit_intent(self):
        for q in ("Actually UI is not that good. Please use /frontend-design skill to polish the UI",
                  "edit the previous file and add a pricing section", "change the colors"):
            self.assertEqual(file_followup_intent(q, True), "edit", q)

    def test_unrelated_followup_is_not_file(self):
        self.assertIsNone(file_followup_intent("what is the capital of Bangladesh?", True))
        self.assertIsNone(file_followup_intent("can you add 2 and 3", True))


class ShrinkToolResultsTests(unittest.TestCase):
    def test_older_results_trimmed_latest_kept(self):
        big = "x" * 20000
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "q"}]
        for i in range(4):
            msgs.append({"role": "assistant", "content": "", "tool_calls": [
                {"id": f"c{i}", "type": "function", "function": {"name": "web_search", "arguments": "{}"}}]})
            msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": big})
        shrink_old_tool_results(msgs, budget_tokens=15000)
        tools = [m for m in msgs if m.get("role") == "tool"]
        self.assertEqual(len(tools), 4)
        self.assertTrue(all(len(t["content"]) < 1000 for t in tools[:2]))
        self.assertTrue(all(len(t["content"]) == 20000 for t in tools[2:]))

    def test_under_budget_untouched(self):
        msgs = [{"role": "user", "content": "q"}, {"role": "tool", "content": "short"}]
        shrink_old_tool_results(msgs, budget_tokens=10000)
        self.assertEqual(msgs[1]["content"], "short")


class KBGateTests(unittest.TestCase):
    """search_knowledge_hybrid used to min-max normalise and always return
    top-k, so an unrelated query still got ~1.0-scored company chunks."""

    ENTRIES = [
        ("knowledge", "knowledge:1:0", "Employee leave policy: 20 days annual leave.", [1.0, 0.0]),
        ("knowledge", "knowledge:1:1", "Payroll is processed on the 25th.", [0.9, 0.1]),
    ]

    def _run(self, query, qvec, titles=None):
        async def fake_embed(texts):
            return [qvec]
        titles = titles or []
        with mock.patch.object(memory, "_load_entries", return_value=self.ENTRIES), \
             mock.patch.object(memory, "_embed_texts", side_effect=fake_embed), \
             mock.patch.object(memory, "_knowledge_id_from_path", return_value=1), \
             mock.patch("core.auth_db.list_knowledge_sources", return_value=titles):
            return asyncio.run(memory.search_knowledge_hybrid(query, k=6, allowed_knowledge_source_ids={1}))

    def test_unrelated_query_gets_nothing(self):
        # orthogonal to every chunk -> raw cosine 0 -> gated out
        self.assertEqual(self._run(MARKET_Q, [0.0, 1.0]), [])

    def test_related_query_passes_with_raw_cos(self):
        hits = self._run("annual leave policy", [1.0, 0.0])
        self.assertTrue(hits)
        self.assertGreaterEqual(hits[0]["cos"], 0.9)

    def test_title_word_is_whole_word_only(self):
        titles = [{"id": 1, "status": "ready", "title": "Online Handbook"}]
        # "online" as a whole word matches the title -> gate bypassed
        self.assertTrue(self._run("online handbook", [0.0, 1.0], titles))
        # "onlinegaming" must not substring-match "online"
        self.assertEqual(self._run("onlinegaming stats", [0.0, 1.0], titles), [])


class CompanyQueryTests(unittest.TestCase):
    def _q(self, text):
        with mock.patch.object(knowledge_router, "_get_active_source_titles", return_value={}):
            return knowledge_router.is_company_or_kb_query(text, {1})

    def test_market_question_not_company(self):
        self.assertFalse(self._q(MARKET_Q))
        self.assertFalse(self._q("walk me through the temperature settings"))  # "hr", "emp" substrings

    def test_company_questions(self):
        self.assertTrue(self._q("What is the leave policy?"))
        self.assertTrue(self._q("show HR contacts"))
        self.assertTrue(self._q("SSL Wireless headquarters address"))


if __name__ == "__main__":
    unittest.main()
