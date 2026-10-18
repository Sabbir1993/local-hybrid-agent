"""tests/test_kb_guard.py - knowledge base: no guardrail at upload, guard on every read.

Run: python -m unittest tests.test_kb_guard -v
Card numbers are public issuer test numbers.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import kb_guard, knowledge_router
from core.small_model import APP_CONFIG

VISA = "4111 1111 1111 1111"
VISA_BN = "৪১১১ ১১১১ ১১১১ ১১১১"


class GuardTextTests(unittest.TestCase):
    def test_masks_pan_and_cvv(self):
        out = kb_guard.guard_text(f"card {VISA} cvv 123")
        self.assertNotIn("4111", out)
        self.assertIn("****1111", out)
        self.assertNotIn("123", out)

    def test_bangla_digits(self):
        out = kb_guard.guard_text(f"কার্ড নম্বর {VISA_BN}")
        self.assertIn("****1111", out)

    def test_clean_text_untouched(self):
        s = "Leave policy: 20 days. Call 01712345678. NID 1990123456789."
        self.assertEqual(kb_guard.guard_text(s), s)

    def test_switch_off(self):
        old = APP_CONFIG.get("pci")
        try:
            APP_CONFIG["pci"] = {"pan_kb_read": "off"}
            self.assertEqual(kb_guard.guard_text(VISA), VISA)
        finally:
            if old is None:
                APP_CONFIG.pop("pci", None)
            else:
                APP_CONFIG["pci"] = old

    def test_non_text_passthrough(self):
        self.assertIsNone(kb_guard.guard_text(None))
        self.assertEqual(kb_guard.guard_text(""), "")


class ReadPathTests(unittest.TestCase):
    def test_prompt_block_masks_stored_text(self):
        hits = [{"title": "Cards", "source_id": 1, "score": 0.9, "cos": 0.9, "text": f"Customer card {VISA} exp 12/28"}]

        async def fake_search(*a, **kw):
            return hits

        async def noop():
            return None

        with mock.patch.object(knowledge_router, "search_knowledge_hybrid", fake_search), \
             mock.patch.object(knowledge_router, "ensure_knowledge_vectors", noop):
            got, block = asyncio.run(knowledge_router.fetch_company_knowledge("card", {1}))
        self.assertNotIn("4111 1111", block)
        self.assertIn("****1111", block)
        self.assertIn(VISA, got[0]["text"])        # raw hits stay for residency decisions, never rendered

    def test_search_tool_masks(self):
        from core.agent_tools import search_tools
        hits = [{"title": "Cards", "source_id": 1, "score": 0.9, "text": f"pan {VISA}"}]

        async def fake_fetch(*a, **kw):
            return hits, ""

        with mock.patch("core.knowledge_access.allowed_source_ids_for", lambda u: {1}), \
             mock.patch("core.knowledge_access.kb_cloud_blocked", lambda: False), \
             mock.patch("core.knowledge_access.partial_notice", lambda ids: ""), \
             mock.patch("core.knowledge_router.fetch_company_knowledge", fake_fetch):
            out = asyncio.run(search_tools.tool_search_knowledge_base({"query": "card number"}))
        self.assertNotIn("4111 1111", out)
        self.assertIn("****1111", out)


if __name__ == "__main__":
    unittest.main()
