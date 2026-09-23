"""tests/test_kb_residency.py - knowledge base never reaches cloud lanes.

Run: python -m unittest tests.test_kb_residency -v
"""

import asyncio
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import knowledge_access as ka
from core import agent_tools
from core.small_model import APP_CONFIG


class KBResidencyTests(unittest.TestCase):
    def setUp(self):
        self._cfg = APP_CONFIG.get("knowledge")

    def tearDown(self):
        if self._cfg is None:
            APP_CONFIG.pop("knowledge", None)
        else:
            APP_CONFIG["knowledge"] = self._cfg

    def test_tool_refuses_when_cloud_lane_active(self):
        APP_CONFIG["knowledge"] = {"cloud_policy": "local_only"}

        async def main():
            self.assertTrue(ka.set_kb_cloud_blocked(True))
            return await agent_tools.tool_search_knowledge_base({"query": "salary of employee"})

        self.assertEqual(asyncio.run(main()), ka.KB_CLOUD_BLOCKED_MSG)

    def test_local_lane_not_blocked(self):
        APP_CONFIG["knowledge"] = {"cloud_policy": "local_only"}

        async def main():
            return ka.set_kb_cloud_blocked(False), ka.kb_cloud_blocked()

        self.assertEqual(asyncio.run(main()), (False, False))

    def test_allow_policy_disables_block(self):
        APP_CONFIG["knowledge"] = {"cloud_policy": "allow"}

        async def main():
            return ka.set_kb_cloud_blocked(True)

        self.assertFalse(asyncio.run(main()))

    def test_default_policy_is_local_only(self):
        APP_CONFIG.pop("knowledge", None)
        self.assertTrue(ka.kb_local_only())

    def test_block_is_per_request(self):
        APP_CONFIG["knowledge"] = {"cloud_policy": "local_only"}

        async def req(cloud):
            ka.set_kb_cloud_blocked(cloud)
            await asyncio.sleep(0.01)
            return ka.kb_cloud_blocked()

        async def main():
            return await asyncio.gather(req(True), req(False))

        self.assertEqual(asyncio.run(main()), [True, False])


class ChatRunBlockedTests(unittest.TestCase):
    """Cloud main lane + KB hit + local model can't start: KB text is withheld
    and the UI gets an explicit kb_blocked event with the reason."""

    SECRET = "Employee headcount is [PLACEHOLDER_HEADCOUNT]."

    def setUp(self):
        self._cfg = APP_CONFIG.get("knowledge")
        APP_CONFIG["knowledge"] = {"cloud_policy": "local_only"}

    def tearDown(self):
        if self._cfg is None:
            APP_CONFIG.pop("knowledge", None)
        else:
            APP_CONFIG["knowledge"] = self._cfg

    def test_kb_blocked_event_and_no_kb_text(self):
        from unittest import mock
        from types import SimpleNamespace
        from routes import chat as chat_route

        lane = SimpleNamespace(model_id="cloud-x", display="cloud-x", provider_name="p")
        hits = [{"title": "HR", "cos": 0.9, "text": self.SECRET}]
        sent = []

        async def fake_stream(client, msgs, **kw):
            sent.append(json.dumps(msgs))
            raise RuntimeError("stop after capturing the cloud payload")
            yield  # pragma: no cover - makes this an async generator

        async def boom(*a, **kw):
            raise RuntimeError("llama-server failed to bind port")

        user = SimpleNamespace(id=1, is_super_admin=True, role_names=[])
        req = chat_route.ChatRunRequest(messages=[
            {"role": "user", "content": "how many employees does this company have?"},
            {"role": "assistant", "content": "Which company?"},
            {"role": "user", "content": "Yes"},
        ], web_search=False)

        async def main():
            with (mock.patch.object(chat_route.cloud, "cloud_lane", return_value=lane),
                  mock.patch.object(chat_route.cloud, "CloudClient"),
                  mock.patch.object(chat_route.input_guard, "check_async", mock.AsyncMock(return_value=None)),
                  mock.patch.object(chat_route, "allowed_source_ids_for", return_value={1}),
                  mock.patch("core.knowledge_router.fetch_company_knowledge",
                             mock.AsyncMock(return_value=(hits, "KB BLOCK: " + self.SECRET))) as fck,
                  mock.patch.object(chat_route.state, "ensure_running", side_effect=boom),
                  mock.patch.object(chat_route.state, "is_running", return_value=False),
                  mock.patch.object(chat_route.state, "profile_path", "profile.json"),
                  mock.patch.object(chat_route, "audit_log") as audit,
                  mock.patch.object(chat_route, "monitor_begin", return_value=1),
                  mock.patch.object(chat_route, "monitor_end", create=True),
                  mock.patch.object(chat_route, "_llm_chat_stream", fake_stream)):
                resp = await chat_route.chat_run(req, user)
                events = []
                try:
                    async for chunk in resp.body_iterator:
                        events.append(chunk)
                        if len(events) > 20:
                            break
                except Exception:
                    pass
                return events, fck.call_args, audit.call_args_list

        events, fck_args, audits = asyncio.run(main())
        self.assertIn("event: lane", events[0])
        self.assertIn("event: kb_blocked", events[1])
        self.assertIn("failed to bind port", events[1])
        self.assertTrue(sent, "cloud stream was never reached")
        self.assertNotIn("PLACEHOLDER_HEADCOUNT", sent[0])
        self.assertIn("restricted to local models", sent[0])
        # short follow-up "Yes" was routed together with the previous question
        self.assertIn("employees", fck_args.args[0])
        self.assertTrue(any(c.kwargs.get("action") == "knowledge.blocked_cloud" for c in audits))


if __name__ == "__main__":
    unittest.main()
