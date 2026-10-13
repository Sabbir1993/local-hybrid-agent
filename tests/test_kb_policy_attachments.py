"""When the company knowledge base is searched: the user's OWN words decide, attached data comes first.

The reported bug: an attached 'Pending list ... SAP & SSL.xlsx' + 'summarise this data set' searched the KB with the whole
message (file text included), because the keyword 'ssl' and the KB titles' words matched the attachment, not the question."""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import knowledge_router as kr  # noqa: E402

SHEET = ("--- FILE: Pending list Sep_Billing Documents Discrepancy SAP & SSL.xlsx ---\n"
         "Data file: x.xlsx - 1 sheet\n- Employee: text\n- Policy: text; leave policy, HR manager\n"
         "[Spreadsheet profile above: not every row is shown.]\n"
         "--- END Pending list Sep_Billing Documents Discrepancy SAP & SSL.xlsx ---")
TITLES = {1: "Automated Test Document", 2: "Employee Base"}


def msg(text: str, attach: bool = False) -> str:
    return text + ("\n\n" + SHEET if attach else "")


class OwnWordsTests(unittest.TestCase):
    def test_attachment_blocks_are_removed(self):
        self.assertEqual(kr.own_words(msg("summarise this data set", True)), "summarise this data set")
        self.assertTrue(kr.has_attachment_text(msg("x", True)))
        self.assertFalse(kr.has_attachment_text("plain question"))

    def test_agent_attached_files_block_and_unterminated_file_are_removed(self):
        agent = "analyse it\n\n[Attached Files]\n--- ATTACHED FILE: a.xlsx (workspace path: a.xlsx) ---\nbody ssl policy"
        self.assertEqual(kr.own_words(agent), "analyse it")
        cut = "look at this\n--- FILE: big.csv ---\nrow1 ssl\nrow2 policy"        # no END marker (cut off)
        self.assertEqual(kr.own_words(cut), "look at this")

    def test_query_is_capped_and_whitespace_collapsed(self):
        long = "word " * 200
        out = kr.own_words(long)
        self.assertLessEqual(len(out), kr.KB_QUERY_MAX_CHARS)
        self.assertNotIn("  ", kr.own_words("a   b\n\n c"))

    def test_routing_query_ignores_attachments_even_on_short_follow_ups(self):
        msgs = [{"role": "user", "content": msg("what is the leave policy", True)}, {"role": "assistant", "content": "..."},
                {"role": "user", "content": "yes"}]
        q = kr.kb_routing_query(msgs, "yes")
        self.assertEqual(q, "what is the leave policy yes")
        self.assertNotIn("FILE", q)


class GateTests(unittest.TestCase):
    def setUp(self):
        self._t = mock.patch.object(kr, "_get_active_source_titles", lambda ids=None: TITLES)
        self._t.start()

    def tearDown(self):
        self._t.stop()

    def test_file_text_cannot_open_the_gate(self):
        full = msg("summarise this data set", True)
        self.assertTrue(kr.is_company_or_kb_query(full), "the OLD behaviour: file words matched")
        self.assertFalse(kr.is_company_or_kb_query(kr.own_words(full)))

    def test_generic_title_words_do_not_count(self):
        for q in ("summarise this document", "run the automated test data", "list the files"):
            self.assertFalse(kr.is_company_or_kb_query(q), q)
        self.assertTrue(kr.is_company_or_kb_query("what is in the Employee Base"))

    def test_decision_table(self):
        policy = kr.should_consult_kb
        cases = [
            # (own words, has attachments, expected consult, reason)
            ("summarise this data set", True, False, kr.KB_ATTACHMENT_ANALYSIS),
            ("analyse this and plot the trend", True, False, kr.KB_ATTACHMENT_ANALYSIS),
            ("how many rows are pending", True, False, kr.KB_ATTACHMENT_ANALYSIS),
            ("compare this with our leave policy", True, True, kr.KB_COMPANY_INTENT),
            ("does this follow the company policy", True, True, kr.KB_COMPANY_INTENT),
            ("check this against the knowledge base", True, True, kr.KB_COMPANY_INTENT),
            ("what is the leave policy", False, True, kr.KB_COMPANY_INTENT),
            ("who is the engineering manager", False, True, kr.KB_COMPANY_INTENT),
            ("SSL Wireless headquarters address", False, True, kr.KB_COMPANY_INTENT),
            ("hi", False, False, kr.KB_SMALL_TALK),
            ("thanks!", False, False, kr.KB_SMALL_TALK),
            ("", False, False, kr.KB_SMALL_TALK),
        ]
        for text, att, want, why in cases:
            got = policy(text, att)
            self.assertEqual(got, (want, why), f"{text!r} attachments={att}")

    def test_user_override_wins(self):
        self.assertEqual(kr.should_consult_kb("summarise this data set", True, mode="on"), (True, kr.KB_USER_FORCED))
        self.assertEqual(kr.should_consult_kb("what is the leave policy", False, mode="off"), (False, kr.KB_USER_BLOCKED))

    def test_voice_keeps_its_company_only_rule(self):
        self.assertEqual(kr.should_consult_kb("tell me a joke", False, voice_company_only=True), (False, kr.KB_VOICE))
        self.assertTrue(kr.should_consult_kb("what is the leave policy", False, voice_company_only=True)[0])

    def test_deployment_keywords_count_on_attachment_turns_and_the_switch_turns_it_off(self):
        extra = {"knowledge": {"company_keywords": ["megalocorp"]}}
        with mock.patch.dict("core.small_model.APP_CONFIG", extra, clear=False):
            self.assertEqual(kr.should_consult_kb("what does megalocorp say about this", True), (True, kr.KB_COMPANY_INTENT))
        off = {"knowledge": {"attachment_skip_kb": False}}
        with mock.patch.dict("core.small_model.APP_CONFIG", off, clear=False):
            self.assertTrue(kr.should_consult_kb("summarise this data set", True)[0], "rollback switch")


class ToolQueryTests(unittest.TestCase):
    def test_a_pasted_message_becomes_a_short_query(self):
        from core.agent_tools import search_tools
        seen = {}

        async def fake_fetch(query, allowed_source_ids=None, k=6):
            seen["q"] = query
            return [], ""

        with mock.patch("core.knowledge_router.fetch_company_knowledge", fake_fetch), \
                mock.patch("core.knowledge_access.allowed_source_ids_for", lambda u: {1}), \
                mock.patch("core.knowledge_access.kb_cloud_blocked", lambda: False), \
                mock.patch("core.request_context.get_current_user_id", lambda: 1), \
                mock.patch("core.auth._to_principal", lambda uid: object()):
            asyncio.run(search_tools.tool_search_knowledge_base({"query": msg("summarise this data set", True)}))
        self.assertEqual(seen["q"], "summarise this data set")
        with self.assertRaises(ValueError):
            asyncio.run(search_tools.tool_search_knowledge_base({"query": SHEET}))     # nothing left after cleaning

    def test_descriptions_tell_the_model_when_not_to_search(self):
        from core.agent_tools import AGENT_TOOLS
        desc = next(t["function"]["description"] for t in AGENT_TOOLS if t["function"]["name"] == "search_knowledge_base")
        self.assertIn("Do NOT use it to", desc)
        self.assertIn("already in this conversation", desc)


if __name__ == "__main__":
    unittest.main()
