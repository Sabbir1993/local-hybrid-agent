"""tool_choice: when the loop asks for a required tool call, and how the request
layer sends it and recovers when a server or provider rejects the field."""
import asyncio
import json
import unittest

from core import router_policy as rp
from routes.common import llm_stream as ls


class ForceToolCallTests(unittest.TestCase):
    Q = "can you test this app on my browser ? and check is there any issues"

    def test_action_request_is_forced_on_first_step_and_after_a_nudge(self):
        self.assertTrue(rp.force_tool_call("action", 0, 0, self.Q))
        self.assertFalse(rp.force_tool_call("action", 3, 0, self.Q))   # mid-run: model decides
        self.assertTrue(rp.force_tool_call("action", 3, 1, self.Q))    # retry right after a nudge

    def test_text_answers_stay_possible(self):
        self.assertFalse(rp.force_tool_call("creation", 0, 0, "write a poem"))
        self.assertFalse(rp.force_tool_call("question", 0, 0, "what is 2+2"))
        self.assertFalse(rp.force_tool_call("greeting", 0, 0, "hi"))

    def test_knowledge_question_with_an_action_word_is_not_forced(self):
        for q in ("how do I read a file in python?", "Explain how to check disk usage"):
            self.assertEqual(rp.classify_query(q), "action")
            self.assertFalse(rp.force_tool_call("action", 0, 0, q), q)

    def test_kill_switch(self):
        self.assertFalse(rp.force_tool_call("action", 0, 0, self.Q, {"tool_choice_required": False}))


class _Resp:
    def __init__(self, status, lines=(), body=b""):
        self.status_code = status
        self._lines = lines
        self._body = body

    async def aread(self):
        return self._body

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln


class _Ctx:
    def __init__(self, resp):
        self.resp = resp

    async def __aenter__(self):
        return self.resp

    async def __aexit__(self, *a):
        return False


class _Client:
    """Rejects any request carrying tool_choice, accepts the rest."""
    base_url = "http://fake-tool-choice-test"

    def __init__(self, reject_status=400):
        self.sent = []
        self.reject_status = reject_status

    def stream(self, method, url, json=None, timeout=None):
        self.sent.append(dict(json))
        if "tool_choice" in json:
            return _Ctx(_Resp(self.reject_status, body=b'{"error":"unknown field tool_choice"}'))
        ok = 'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}]}'
        return _Ctx(_Resp(200, lines=[ok, "data: [DONE]"]))


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


async def _drain(gen):
    return [x async for x in gen]


TOOLS = [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}]


class RequestPlumbingTests(unittest.TestCase):
    def setUp(self):
        ls._TOOL_CHOICE_UNSUPPORTED.clear()

    def test_sent_when_tools_are_offered(self):
        c = _Client()
        c.reject_status = 0   # never reject
        c.stream = lambda m, u, json=None, timeout=None: (
            c.sent.append(dict(json)) or _Ctx(_Resp(200, lines=["data: [DONE]"])))
        _run(_drain(ls._llm_chat_stream_raw(c, [], TOOLS, tool_choice="required")))
        self.assertEqual(c.sent[0]["tool_choice"], "required")

    def test_not_sent_without_tools(self):
        c = _Client()
        c.stream = lambda m, u, json=None, timeout=None: (
            c.sent.append(dict(json)) or _Ctx(_Resp(200, lines=["data: [DONE]"])))
        _run(_drain(ls._llm_chat_stream_raw(c, [], None, tool_choice="required")))
        self.assertNotIn("tool_choice", c.sent[0])

    def test_rejected_field_is_retried_without_it_and_remembered(self):
        c = _Client(reject_status=400)
        out = _run(_drain(ls._llm_chat_stream_raw(c, [], TOOLS, tool_choice="required")))
        self.assertTrue(out)                                 # the retry produced a reply
        self.assertIn("tool_choice", c.sent[0])
        self.assertNotIn("tool_choice", c.sent[1])
        _run(_drain(ls._llm_chat_stream_raw(c, [], TOOLS, tool_choice="required")))
        self.assertNotIn("tool_choice", c.sent[2])           # not asked again


if __name__ == "__main__":
    unittest.main()
