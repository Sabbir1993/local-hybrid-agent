"""tests/test_stream.py - unit tests for the shared lane-stream drain and accounting.

Run: python -m unittest tests.test_stream -v

core/agent_loop/stream.py holds the single copy of the stream+redact+account block that
used to live in four near-verbatim copies inside routes/agent/run.py (greeting, normal
step, escalation replay, wrap-up). The escalation copy had already diverged in two places
nobody noticed. These tests pin the unified behavior, including the two transcribed
non-uniformities, using a scripted event stream and the real OutputRedactor.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.agent_loop import stream as st
from core.agent_loop.stream import (
    AccountResult,
    DrainResult,
    StreamSpec,
    account_llm_result,
    drain_llm_stream,
    guard_audit,
    guard_flush_events,
)
from core.monitor import _monitor_state


def run(coro):
    return asyncio.run(coro)


async def fake_stream(events):
    for ev in events:
        yield ev


def spec(**kw):
    base = dict(step=3, model_display="M", audit_label="agent/test", collect=[],
                fallback_payload={"display": "L", "model": "m", "source": "local"})
    base.update(kw)
    return StreamSpec(**base)


class DrainEvents(unittest.TestCase):
    def drain(self, events, mock_user=None, **kw):
        # a bare Mock breaks the real redactor's rule lookup; use a Principal-shaped
        # namespace instead so feed/flush/matched execute for real
        import types
        user = mock_user if mock_user is not None else types.SimpleNamespace(
            id=7, username="u7", is_super_admin=False, role_names=[],
            permission_keys={"chat.use"})
        out = DrainResult()
        async def go():
            chunks = []
            async for chunk in drain_llm_stream(fake_stream(events), user, spec(**kw),
                                                {"display": "M"}, False, out):
                chunks.append(chunk)
            return chunks
        return run(go()), out

    def test_queued_passes_through(self):
        chunks, out = self.drain([("queued", {"position": 2})])
        self.assertEqual(len(chunks), 1)
        self.assertIn("queued", chunks[0])
        self.assertIsNone(out.res_dict)

    def test_thought_delta_carries_step_and_model(self):
        chunks, _ = self.drain([("thought_delta", "hmm")])
        self.assertTrue(any('"step": 3' in c and "hmm" in c for c in chunks))

    def test_content_delta_streams_text(self):
        # the holdback may split one delta across chunks; join the text payloads
        import json
        chunks, _ = self.drain([("content_delta", "hello ")])
        texts = "".join(
            json.loads(c.split("data: ", 1)[1]).get("text", "")
            for c in chunks if c.startswith("event: delta\n"))
        self.assertEqual(texts, "hello ")

    def test_content_to_thought_resets_and_clears(self):
        collected = ["stale chunk"]
        chunks, _ = self.drain([("content_to_thought", None)], collect=collected)
        self.assertEqual(collected, [], "forced-open think discards the misread text")
        self.assertTrue(any("delta_to_thought" in c for c in chunks))

    def test_tool_preparing_forwarded_by_default(self):
        chunks, _ = self.drain([("tool_preparing", {"name": "read_file"})])
        self.assertTrue(any("tool_preparing" in c and "read_file" in c for c in chunks))

    def test_tool_preparing_dropped_for_greeting_shape(self):
        chunks, _ = self.drain([("tool_preparing", {"name": "read_file"})],
                               forward_preparing=False)
        self.assertFalse(any("tool_preparing" in c for c in chunks))

    def test_result_captured_not_yielded(self):
        res = {"content": "done", "tool_calls": []}
        chunks, out = self.drain([("result", res)])
        self.assertEqual(out.res_dict, res)
        self.assertFalse(any("done" in c and "tool_calls" in c for c in chunks))

    def test_unknown_events_pass_silently(self):
        chunks, out = self.drain([("something_new", {"x": 1})])
        self.assertEqual(chunks, [])
        self.assertIsNone(out.res_dict)

    def test_fallback_emits_lane_and_resets(self):
        chunks, out = self.drain([("content_delta", "hi"),
                                  ("fallback", {}),
                                  ("content_delta", "there")])
        self.assertTrue(out.fell_back)
        lane_chunks = [c for c in chunks if "event: lane" in c]
        self.assertEqual(len(lane_chunks), 1)
        self.assertIn("L", lane_chunks[0])  # fallback payload, adopted
        self.assertEqual(out.model_info["display"], "L")

    def test_escalation_shape_keeps_original_model(self):
        # transcribed non-uniformity: the escalation copy never adopted the local
        # model info for later events, while greeting/normal did
        chunks, out = self.drain([("fallback", {}), ("thought_delta", "hmm")],
                                 adopt_fallback_model=False)
        self.assertTrue(out.fell_back)
        self.assertEqual(out.model_info["display"], "M")
        self.assertTrue(any('"model": "M"' in c for c in chunks))

    def test_flush_appends_tail_and_audits(self):
        # a PAN in the stream: the holdback keeps it back mid-stream, flush releases the
        # masked tail plus a guard event, and the match is audited
        import types
        user = types.SimpleNamespace(id=7, username="u7", is_super_admin=False,
                                     role_names=[], permission_keys={"chat.use"})
        with mock.patch("core.audit.audit_log") as audit:
            chunks, _ = self.drain([("content_delta", "card 4111 1111 1111 1111 ok")],
                                   mock_user=user)
        audit.assert_called_once()
        args, kwargs = audit.call_args
        self.assertEqual(args[0], user)
        self.assertEqual(kwargs["detail"]["endpoint"], "agent/test")
        self.assertEqual(kwargs["result"], "deny")
        blob = "\n".join(chunks)
        self.assertIn("[card ****1111]", blob)
        self.assertNotIn("4111 1111 1111 1111", blob)
        self.assertTrue(any('"rule"' in c for c in chunks))


class GuardHelpers(unittest.TestCase):
    def test_flush_empty_is_silent(self):
        red = mock.Mock()
        red.flush.return_value = ""
        red.matched = None
        self.assertEqual(guard_flush_events(red, []), [])

    def test_flush_tail_becomes_delta_and_guard_event(self):
        red = mock.Mock()
        red.flush.return_value = "tail text"
        red.matched = {"name": "pan", "message": "card"}
        chunks = guard_flush_events(red, [])
        self.assertTrue(any("tail text" in c for c in chunks))
        self.assertTrue(any('"rule": "pan"' in c for c in chunks))

    def test_audit_only_on_match(self):
        with mock.patch("core.audit.audit_log") as log:
            guard_audit(mock.Mock(matched=None), mock.Mock(), "agent/x")
            guard_audit(mock.Mock(matched={"name": "r", "scope": "s", "hits": 1},
                                  hits=1), mock.Mock(), "agent/x")
        log.assert_called_once()


class Account(unittest.TestCase):
    def setUp(self):
        self.rid = 424242
        _monitor_state["active"].pop(self.rid, None)

    def tearDown(self):
        _monitor_state["active"].pop(self.rid, None)

    def _seed(self, **kw):
        req = {"start": 0.0, "gen_tokens": 0}
        req.update(kw)
        _monitor_state["active"][self.rid] = req

    def test_usage_anchors_record_usage(self):
        self._seed(start=0.0, gen_tokens=10)
        res = {"usage": {"prompt_tokens": 100, "completion_tokens": 20}, "timings": {}}
        with mock.patch.object(st, "db_record_request") as db, \
             mock.patch.object(st, "record_usage") as rec:
            acct = account_llm_result(
                rid=self.rid, res_dict=res, streamed=["x" * 10],
                msgs=[{"content": "a" * 100}], model_info={"model": "m", "source": "local"},
                endpoint="agent/executor", usage_lane="executor", usage_tools=[],
                sent_tokens=50, is_orchestrator=True, run_id="r1")
        self.assertEqual((acct.ptoks, acct.ctoks), (100, 20))
        rec.assert_called_once()
        args, _ = rec.call_args
        self.assertEqual(args[:3], ("executor", 50, 100))
        db.assert_called_once()
        kwargs = db.call_args[1]
        self.assertEqual(kwargs.get("run_id"), "r1")
        self.assertTrue(kwargs.get("is_orchestrator"))

    def test_fallback_estimate_never_anchors(self):
        # no usage from the server: the chars//4 guess must not feed the budget learner
        self._seed(start=0.0, gen_tokens=10)
        with mock.patch.object(st, "db_record_request"), \
             mock.patch.object(st, "record_usage") as rec:
            acct = account_llm_result(
                rid=self.rid, res_dict={}, streamed=["x" * 10],
                msgs=[{"content": "a" * 400}], model_info={"model": "m", "source": "local"},
                endpoint="agent/executor", usage_lane="executor", usage_tools=[],
                sent_tokens=50)
        rec.assert_not_called()
        self.assertEqual(acct.ptoks, 100)  # 400 chars // 4

    def test_greeting_shape_is_completion_only(self):
        self._seed(start=0.0, gen_tokens=7)
        with mock.patch.object(st, "db_record_request") as db, \
             mock.patch.object(st, "record_usage") as rec:
            acct = account_llm_result(
                rid=self.rid, res_dict=None, streamed=None,
                msgs=[{"content": "hi"}], model_info={"model": "m", "source": "local"},
                endpoint=None)
        db.assert_not_called()
        rec.assert_not_called()
        self.assertEqual(acct.ctoks, 7)
        self.assertIsNone(acct.ptoks)

    def test_cache_figures_are_never_invented(self):
        # a provider that reports no cache must not read as a 90%+ hit rate
        self._seed(start=0.0, gen_tokens=10)
        with mock.patch.object(st, "db_record_request"), \
             mock.patch.object(st, "record_usage"):
            acct = account_llm_result(
                rid=self.rid, res_dict={"usage": {"prompt_tokens": 100}}, streamed=[],
                msgs=[{"content": "a"}, {"content": "b"}],
                model_info={"model": "m", "source": "cloud"},
                endpoint="agent/main", usage_lane="main", usage_tools=[], sent_tokens=10)
        self.assertEqual(acct.pcached, 0)

    def test_local_prefix_fabrication_preserved(self):
        self._seed(start=0.0, gen_tokens=10)
        with mock.patch.object(st, "db_record_request"), \
             mock.patch.object(st, "record_usage"):
            acct = account_llm_result(
                rid=self.rid, res_dict={"usage": {"prompt_tokens": 100}}, streamed=[],
                msgs=[{"content": "a" * 400}, {"content": "b"}],
                model_info={"model": "m", "source": "local"},
                endpoint="agent/main", usage_lane="main", usage_tools=[], sent_tokens=10)
        self.assertEqual(acct.pcached, 100)  # first message's chars // 4


if __name__ == "__main__":
    unittest.main()
