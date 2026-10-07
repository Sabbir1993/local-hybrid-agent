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


class PerSourceCloudTests(unittest.TestCase):
    """The admin marks individual sources as readable by cloud models (knowledge_sources.cloud_ok)."""

    def setUp(self):
        self._cfg = APP_CONFIG.get("knowledge")
        APP_CONFIG["knowledge"] = {"cloud_policy": "local_only"}

    def tearDown(self):
        if self._cfg is None:
            APP_CONFIG.pop("knowledge", None)
        else:
            APP_CONFIG["knowledge"] = self._cfg

    def test_hits_need_local_unless_every_source_cleared(self):
        from unittest import mock
        with mock.patch.object(ka.auth_db, "cloud_ok_source_ids", return_value={2, 3}):
            self.assertFalse(ka.hits_need_local([{"source_id": 2}, {"source_id": 3}]))
            self.assertTrue(ka.hits_need_local([{"source_id": 2}, {"source_id": 9}]))
            self.assertTrue(ka.hits_need_local([{"title": "no id"}]))
            self.assertEqual(ka.cloud_ok_ids({1, 2, 3}), {2, 3})
        APP_CONFIG["knowledge"] = {"cloud_policy": "allow"}
        self.assertFalse(ka.hits_need_local([{"source_id": 9}]))
        self.assertEqual(ka.cloud_ok_ids({1, 2}), {1, 2})

    def test_tool_on_cloud_lane_searches_only_cleared_sources(self):
        from unittest import mock
        seen = {}

        async def fake_fetch(q, allowed_source_ids, k=6):
            seen["ids"] = set(allowed_source_ids)
            return [], ""

        async def main():
            ka.set_kb_cloud_blocked(True)
            return await agent_tools.tool_search_knowledge_base({"query": "refund policy"})

        with (mock.patch.object(ka.auth_db, "cloud_ok_source_ids", return_value={2}),
              mock.patch.object(ka, "allowed_source_ids_for", return_value={1, 2}),
              mock.patch("core.knowledge_router.fetch_company_knowledge", fake_fetch)):
            asyncio.run(main())
        self.assertEqual(seen["ids"], {2})


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

    def _run_blocked(self, web, hits=None, **req_kw):
        from unittest import mock
        from types import SimpleNamespace
        from routes import chat as chat_route

        lane = SimpleNamespace(model_id="cloud-x", display="cloud-x", provider_name="p")
        hits = hits or [{"title": "HR", "cos": 0.9, "text": self.SECRET}]
        sent = []
        tools_seen = []

        async def fake_stream(client, msgs, **kw):
            sent.append(json.dumps(msgs))
            tools_seen.append([t.get("function", {}).get("name") for t in (kw.get("tools") or [])])
            raise RuntimeError("stop after capturing the cloud payload")
            yield  # pragma: no cover - makes this an async generator

        async def boom(*a, **kw):
            raise RuntimeError("llama-server failed to bind port")

        user = SimpleNamespace(id=1, is_super_admin=True, role_names=[])
        req = chat_route.ChatRunRequest(messages=[
            {"role": "user", "content": "how many employees does this company have?"},
            {"role": "assistant", "content": "Which company?"},
            {"role": "user", "content": "Yes"},
        ], web_search=web, **req_kw)

        async def main():
            with (mock.patch.object(chat_route.run.cloud, "cloud_lane", return_value=lane),
                  mock.patch.object(chat_route.run.cloud, "CloudClient"),
                  mock.patch.object(chat_route.run.input_guard, "check_async", mock.AsyncMock(return_value=None)),
                  mock.patch.object(chat_route.run, "allowed_source_ids_for", return_value={1}),
                  mock.patch("core.knowledge_router.fetch_company_knowledge",
                             mock.AsyncMock(return_value=(hits, "KB BLOCK: " + self.SECRET))) as fck,
                  mock.patch.object(chat_route.run.state, "ensure_running", side_effect=boom),
                  mock.patch.object(chat_route.run.state, "is_running", return_value=False),
                  mock.patch.object(chat_route.run.state, "profile_path", "profile.json"),
                  mock.patch.object(chat_route.run, "audit_log") as audit,
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
                return events, fck.call_args, audit.call_args_list

        events, fck_args, audits = asyncio.run(main())
        return events, fck_args, audits, sent, tools_seen

    def test_kb_blocked_event_and_no_kb_text(self):
        events, fck_args, audits, sent, tools_seen = self._run_blocked(False)
        self.assertIn("event: lane", events[0])
        self.assertIn("event: kb_blocked", events[1])
        self.assertIn("failed to bind port", events[1])
        self.assertTrue(sent, "cloud stream was never reached")
        self.assertNotIn("PLACEHOLDER_HEADCOUNT", sent[0])
        self.assertIn("restricted to local models", sent[0])
        # short follow-up "Yes" was routed together with the previous question
        self.assertIn("employees", fck_args.args[0])
        self.assertTrue(any(c.kwargs.get("action") == "knowledge.blocked_cloud" for c in audits))

        self.assertFalse(any("web_search" in t for t in tools_seen[0]))
        self.assertNotIn("public web", sent[0])

    def test_web_search_is_offered_when_the_kb_is_blocked(self):
        from unittest import mock
        with mock.patch.dict(APP_CONFIG.setdefault("capabilities", {}), {"web": True}):
            events, _, _, sent, tools_seen = self._run_blocked(True)
        self.assertIn("event: kb_blocked", events[1])
        self.assertNotIn("PLACEHOLDER_HEADCOUNT", sent[0])
        self.assertIn("web_search", tools_seen[0])
        self.assertIn("never present web results as company records", sent[0])


    def test_strong_kb_match_keeps_web_off_even_when_blocked(self):
        """A KB that clearly covers the question never pays for the web tools (blocked or not)."""
        from unittest import mock
        strong = [{"title": "HR", "cos": 0.9, "text": self.SECRET}, {"title": "HR2", "cos": 0.8, "text": "x"}]
        with mock.patch.dict(APP_CONFIG.setdefault("capabilities", {}), {"web": True}):
            events, _, _, sent, tools_seen = self._run_blocked(True, hits=strong)
        self.assertFalse(any("web" in (t or "") for t in tools_seen[0]), tools_seen[0])
        self.assertNotIn("WEB (gap-filling)", sent[0])
        self.assertNotIn("LIVE REAL-TIME INTERNET", sent[0])

    def test_deep_mode_gets_a_bigger_budget_and_more_rounds(self):
        from unittest import mock
        weak = [{"title": "HR", "cos": 0.6, "text": self.SECRET}]
        with mock.patch.dict(APP_CONFIG.setdefault("capabilities", {}), {"web": True}):
            _, _, _, sent_med, _ = self._run_blocked(True, hits=weak)
            _, _, _, sent_deep, _ = self._run_blocked(True, hits=weak, deep_mode=True)
        self.assertIn("at most 3 call(s)", sent_med[0])
        self.assertIn("at most 12 call(s)", sent_deep[0])


if __name__ == "__main__":
    unittest.main()
