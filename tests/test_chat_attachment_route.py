"""The chat route with an attached spreadsheet: no KB lookup from file text, the model gets data_query and the data prompt,
and the user's /kb switch (kb = auto | on | off) is honoured. Same harness as tests/test_kb_residency.py."""
import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.small_model import APP_CONFIG  # noqa: E402

SHEET = ("--- FILE: Pending list Sep_Billing Documents Discrepancy SAP & SSL.xlsx ---\n"
         "Data file: x.xlsx - 1 sheet, 300 data rows\n## Sheet \"Pending\"\n- Employee: text; leave policy HR manager\n"
         "[Spreadsheet profile above: not every row is shown. For exact counts call data_query(file=\"x-1a2b3c4d.xlsx\").]\n"
         "--- END Pending list Sep_Billing Documents Discrepancy SAP & SSL.xlsx ---")
HIT = [{"title": "Automated Test Document", "cos": 0.5, "text": "KB TEXT"}]


class ChatAttachmentRouteTests(unittest.TestCase):
    def setUp(self):
        self._cfg = APP_CONFIG.get("knowledge")
        APP_CONFIG["knowledge"] = {"cloud_policy": "allow", "company_keywords": ["ssl"]}

    def tearDown(self):
        if self._cfg is None:
            APP_CONFIG.pop("knowledge", None)
        else:
            APP_CONFIG["knowledge"] = self._cfg

    def _run(self, text, attach=True, kb="auto", history=None):
        from routes import chat as chat_route
        lane = SimpleNamespace(model_id="cloud-x", display="cloud-x", provider_name="p")
        sent, tools_seen = [], []

        async def fake_stream(client, msgs, **kw):
            sent.append(json.dumps(msgs))
            tools_seen.append([t.get("function", {}).get("name") for t in (kw.get("tools") or [])])
            raise RuntimeError("stop after capturing the payload")
            yield  # pragma: no cover

        user = SimpleNamespace(id=1, is_super_admin=True, role_names=[])
        content = text + ("\n\n" + SHEET if attach else "")
        req = chat_route.ChatRunRequest(messages=(history or []) + [{"role": "user", "content": content}], web_search=False, kb=kb)

        async def main():
            with (mock.patch.object(chat_route.run.cloud, "cloud_lane", return_value=lane),
                  mock.patch.object(chat_route.run.cloud, "CloudClient"),
                  mock.patch.object(chat_route.run.input_guard, "check_async", mock.AsyncMock(return_value=None)),
                  mock.patch.object(chat_route.run, "allowed_source_ids_for", return_value={1}),
                  mock.patch("core.knowledge_router._get_active_source_titles",
                             return_value={1: "Automated Test Document", 2: "Employee Base"}),
                  mock.patch("core.knowledge_router.fetch_company_knowledge",
                             mock.AsyncMock(return_value=(HIT, "KB BLOCK: KB TEXT"))) as fck,
                  mock.patch.object(chat_route.run.state, "is_running", return_value=True),
                  mock.patch.object(chat_route.run, "audit_log"),
                  mock.patch.object(chat_route.run, "monitor_begin", return_value=1),
                  mock.patch.object(chat_route.run, "monitor_end", create=True),
                  mock.patch.object(chat_route.run, "_llm_chat_stream", fake_stream)):
                resp = await chat_route.chat_run(req, user)
                events = []
                try:
                    async for chunk in resp.body_iterator:
                        events.append(chunk)
                        if len(events) > 20:
                            break
                except Exception:
                    pass
                return events, fck

        events, fck = asyncio.run(main())
        return events, fck, sent, tools_seen

    def test_summarise_an_attached_sheet_does_not_search_the_kb(self):
        events, fck, sent, tools = self._run("summarise this data set")
        self.assertEqual(fck.call_count, 0, "the file name 'SSL' and the sheet text must not trigger a KB lookup")
        self.assertFalse(any("kb_fetch_0" in e for e in events), "no automatic retrieval card")
        self.assertTrue(sent, "the model was never called")
        self.assertIn("ATTACHED DATA SET", sent[0])
        self.assertNotIn("KB TEXT", sent[0])
        self.assertIn("data_query", tools[0])
        self.assertIn("search_knowledge_base", tools[0], "the model can still look in the KB on demand")

    def test_a_company_question_about_the_sheet_still_searches(self):
        events, fck, sent, tools = self._run("compare this with our leave policy")
        self.assertEqual(fck.call_count, 1)
        self.assertEqual(fck.call_args.args[0], "compare this with our leave policy", "the short own-words question is the query")
        self.assertIn("data_query", tools[0])

    def test_company_question_without_attachment_searches_with_a_short_query(self):
        events, fck, sent, tools = self._run("what is the leave policy", attach=False)
        self.assertEqual(fck.call_count, 1)
        self.assertEqual(fck.call_args.args[0], "what is the leave policy")
        self.assertNotIn("data_query", tools[0])
        self.assertNotIn("ATTACHED DATA SET", sent[0])

    def test_user_switch_overrides_the_policy(self):
        _, fck_on, _, _ = self._run("summarise this data set", kb="on")
        self.assertEqual(fck_on.call_count, 1, "Always: searched even for a file analysis")
        _, fck_off, _, _ = self._run("what is the leave policy", attach=False, kb="off")
        self.assertEqual(fck_off.call_count, 0, "Off: never searched")

    def test_follow_up_question_keeps_data_query_available(self):
        hist = [{"role": "user", "content": "summarise this data set\n\n" + SHEET},
                {"role": "assistant", "content": "It is a pending billing list."}]
        _, fck, sent, tools = self._run("which status has the most rows", attach=False, history=hist)
        self.assertIn("data_query", tools[0])
        self.assertIn("ATTACHED DATA SET", sent[0])
        self.assertEqual(fck.call_count, 0, 'a follow-up about the sheet is still data analysis, not a KB search')


if __name__ == "__main__":
    unittest.main()
