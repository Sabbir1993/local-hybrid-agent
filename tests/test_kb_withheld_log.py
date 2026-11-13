"""tests/test_kb_withheld_log.py - a retrieval withheld by the auto_inject_cos gate is still logged with its real scores.

Run: python -m unittest tests.test_kb_withheld_log -v
"""

import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.small_model import APP_CONFIG  # noqa: E402

SECRET = "Quarterly bonus pool is [PLACEHOLDER_BONUS]."


class KbWithheldLogTests(unittest.TestCase):
    def setUp(self):
        self._cfg = APP_CONFIG.get("knowledge")
        APP_CONFIG["knowledge"] = {"auto_inject_cos": 0.55, "cloud_policy": "local_only"}

    def tearDown(self):
        if self._cfg is None:
            APP_CONFIG.pop("knowledge", None)
        else:
            APP_CONFIG["knowledge"] = self._cfg

    def _run_chat(self, hits, prompt_block):
        from routes import chat as chat_route

        sent = []

        async def fake_stream(client, msgs, **kw):
            sent.append(json.dumps(msgs))
            raise RuntimeError("stop after capturing the prompt")
            yield  # pragma: no cover

        user = SimpleNamespace(id=7, is_super_admin=True, role_names=[])
        req = chat_route.ChatRunRequest(messages=[
            {"role": "user", "content": "what is the weather like in Sylhet today"},
        ], web_search=False)
        recorded = []

        async def main():
            with (mock.patch.object(chat_route.run.state, "process", SimpleNamespace(poll=lambda: None)),
                  mock.patch.object(chat_route.run.state, "client", object()),
                  mock.patch.object(chat_route.run.input_guard, "check_async", mock.AsyncMock(return_value=None)),
                  mock.patch.object(chat_route.run, "allowed_source_ids_for", return_value={1}),
                  mock.patch("core.knowledge_router.should_consult_kb", return_value=(True, "test")),
                  mock.patch("core.knowledge_router.fetch_company_knowledge",
                             mock.AsyncMock(return_value=(hits, prompt_block))),
                  mock.patch("core.kb_log.record", side_effect=lambda *a, **k: recorded.append((a, k))),
                  mock.patch.object(chat_route.run, "monitor_begin", return_value=1),
                  mock.patch.object(chat_route.run, "monitor_end", create=True),
                  mock.patch.object(chat_route.run, "db_record_request"),
                  mock.patch.object(chat_route.run, "_llm_chat_stream", fake_stream)):
                resp = await chat_route.chat_run(req, user)
                try:
                    async for _ in resp.body_iterator:
                        pass
                except Exception:
                    pass

        asyncio.run(main())
        return recorded, sent

    def test_withheld_retrieval_is_logged_with_real_count_and_top_cos(self):
        hits = [{"title": "HR", "cos": 0.31, "text": SECRET},
                {"title": "Ops", "cos": 0.22, "text": SECRET}]
        recorded, sent = self._run_chat(hits, "KB BLOCK: " + SECRET)
        self.assertTrue(recorded, "kb_log.record was never called")
        hits_n, top, injected = self._fields(recorded[0])
        self.assertEqual(hits_n, 2)
        self.assertAlmostEqual(top, 0.31, places=3)
        self.assertFalse(injected)
        self.assertTrue(sent, "chat stream was never reached")
        self.assertNotIn("PLACEHOLDER_BONUS", sent[0])

    def test_injected_retrieval_is_logged_and_injected(self):
        hits = [{"title": "HR", "cos": 0.81, "text": SECRET}]
        recorded, sent = self._run_chat(hits, "KB BLOCK: " + SECRET)
        self.assertTrue(recorded, "kb_log.record was never called")
        hits_n, top, injected = self._fields(recorded[0])
        self.assertEqual(hits_n, 1)
        self.assertAlmostEqual(top, 0.81, places=3)
        self.assertTrue(injected)

    @staticmethod
    def _fields(call):
        # kb_log.record(user_id, surface, consulted, reason, hits, top_cos, injected)
        args, kwargs = call
        return (args[4] if len(args) > 4 else kwargs.get("hits"),
                args[5] if len(args) > 5 else kwargs.get("top_cos"),
                args[6] if len(args) > 6 else kwargs.get("injected", False))


if __name__ == "__main__":
    unittest.main()
