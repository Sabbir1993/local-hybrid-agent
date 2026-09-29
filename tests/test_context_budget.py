"""Prompt-budget accounting (core/context_budget.py) and the overflow recovery
path in routes/common.py.

Why these tests exist:

  * the agent loop decides when to compact from an *estimate*. That estimate was
    tool-blind (48 tool schemas = ~8.5k tokens went uncounted) and used chars//3,
    so a 36,350-token prompt passed a 22,937-token budget check and the server
    answered 400 exceed_context_size_error - the reported failure.
  * the recovery branch for that 400 sat *after* the grammar-retry and JSON-repair
    branches, both of which raise on their own non-200, so it was unreachable on
    exactly the path an executor lane (grammar enabled) takes.

The tests below pin the window resolution (np / -kvu aware, /slots-authoritative),
the anchored accounting, the root-cause inequality, and the branch order.

Run: python -m unittest tests.test_context_budget -v
"""

import asyncio
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import context_budget as cb  # noqa: E402
from core.agent_loop import estimate_prompt_tokens  # noqa: E402

WINDOW_32K = 32768
BUDGET_32K = 22937          # 32,768 * the pre-existing 0.70 margin, at factor 1.0


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def json(self):
        return self._payload


class _FakeClient:
    """Stands in for an httpx.AsyncClient talking to llama-server."""

    def __init__(self, payload=None, status=200, boom=False):
        self.payload, self.status, self.boom, self.calls = payload, status, boom, 0

    async def get(self, path, timeout=None):
        self.calls += 1
        if self.boom:
            raise RuntimeError("connection refused")
        return _FakeResp(self.payload, self.status)


def _tool_schemas(n=48, desc_chars=300):
    return [{"type": "function",
             "function": {"name": f"tool_{i}", "description": "d" * desc_chars,
                          "parameters": {"type": "object", "properties": {}}}}
            for i in range(n)]


def _history(results=2, result_chars=20000, system_chars=9000):
    """A conversation shaped like the one that overflowed: a large system prompt
    and over-sized tool results (MAX_TOOL_OUTPUT is 20,000 chars per result)."""
    msgs = [{"role": "system", "content": "S" * system_chars},
            {"role": "user", "content": "do the task"}]
    for i in range(results):
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "x" * result_chars})
    return msgs


class TestWindow(unittest.TestCase):

    def setUp(self):
        cb.reset()

    def test_single_slot_uses_whole_pool(self):
        self.assertEqual(cb.window("main", cfg_ctx=WINDOW_32K), WINDOW_32K)

    def test_unified_kv_ignores_slot_count(self):
        """-np 3 with -kvu shares one pool: the per-request window stays -c."""
        self.assertEqual(cb.window("executor", cfg_ctx=32576, n_slots=3, kv_unified=True), 32576)

    def test_parallel_slots_divide_without_unified_kv(self):
        self.assertEqual(cb.window("x", cfg_ctx=12000, n_slots=3), 4000)

    def test_no_config_is_unknown_not_a_zero_floor(self):
        self.assertEqual(cb.window("x", cfg_ctx=0), 0)

    def test_resolution_is_remembered_but_not_as_probed(self):
        cb.window("main", cfg_ctx=WINDOW_32K)
        snap = cb.snapshot()["main"]
        self.assertEqual(snap["window"], WINDOW_32K)
        self.assertFalse(snap["window_is_probed"], "a config value must not claim to be probed")

    def test_probe_overrides_config(self):
        cb.window("main", cfg_ctx=WINDOW_32K)
        client = _FakeClient([{"n_ctx": 8192}])
        self.assertEqual(asyncio.run(cb.probe_window("main", client)), 8192)
        self.assertEqual(cb.window("main", cfg_ctx=WINDOW_32K), 8192)
        self.assertTrue(cb.snapshot()["main"]["window_is_probed"])

    def test_probe_is_cached_and_backs_off(self):
        client = _FakeClient([{"n_ctx": 16384}])
        asyncio.run(cb.probe_window("main", client))
        asyncio.run(cb.probe_window("main", client))
        self.assertEqual(client.calls, 1, "a second probe inside the TTL must be free")
        cb.reset()
        dead = _FakeClient(boom=True)
        self.assertIsNone(asyncio.run(cb.probe_window("main", dead)))
        asyncio.run(cb.probe_window("main", dead))
        self.assertEqual(dead.calls, 1, "a failed probe must back off, not retry every step")

    def test_probe_survives_junk_shapes_and_bad_status(self):
        for client in (_FakeClient({"unexpected": "shape"}),
                       _FakeClient([]),
                       _FakeClient([{"no_ctx": 1}]),
                       _FakeClient([{"n_ctx": 0}]),
                       _FakeClient([{"n_ctx": 1024}], status=500)):
            cb.reset()
            with self.subTest(payload=client.payload, status=client.status):
                self.assertIsNone(asyncio.run(cb.probe_window("main", client)))

    def test_no_client_is_not_an_error(self):
        self.assertIsNone(asyncio.run(cb.probe_window("main", None)))


class TestBudget(unittest.TestCase):

    def setUp(self):
        cb.reset()

    def test_margin_applies(self):
        self.assertEqual(cb.budget_for("main", WINDOW_32K), BUDGET_32K)

    def test_unknown_window_has_no_budget(self):
        self.assertEqual(cb.budget_for("main", 0), 0)

    def test_learned_error_tightens_the_budget(self):
        """If the estimate ran low, the budget must shrink by the same factor so
        compaction still fires before the server refuses."""
        cb.record_usage("main", estimated=1000, actual=1200)
        self.assertGreater(cb.factor("main"), 1.0)
        self.assertLess(cb.budget_for("main", WINDOW_32K), BUDGET_32K)

    def test_factor_never_exceeds_two(self):
        for _ in range(20):
            cb.record_usage("main", estimated=100, actual=100000)
        self.assertLessEqual(cb.factor("main"), 2.0)
        self.assertGreaterEqual(cb.budget_for("main", WINDOW_32K), int(WINDOW_32K * 0.70 / 2.0))


class TestAccounting(unittest.TestCase):

    def setUp(self):
        cb.reset()

    def test_first_step_is_the_plain_estimate(self):
        msgs, tools = _history(), _tool_schemas()
        self.assertEqual(cb.prompt_tokens_for("main", msgs, tools),
                         estimate_prompt_tokens(msgs, tools))

    def test_anchor_is_never_lower_than_the_server_reported(self):
        msgs, tools = _history(), _tool_schemas()
        cb.record_usage("main", estimated=1000, actual=20000, msgs=msgs, tools=tools)
        self.assertGreaterEqual(cb.prompt_tokens_for("main", msgs, tools), 20000)

    def test_growth_is_charged_at_the_learned_ratio(self):
        small = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
        cb.record_usage("main", estimated=10, actual=30000, msgs=small)
        bigger = small + [{"role": "assistant", "content": "z" * 300}]
        got = cb.prompt_tokens_for("main", bigger)
        self.assertGreater(got, 30000, "must grow with the new message")
        self.assertLess(got, 30600, "one 300-char message costs ~100 tokens, not thousands")

    def test_a_shrunken_prompt_drops_the_anchor(self):
        """After compaction the history is smaller than the anchored request, so
        the anchor must not keep claiming the old size."""
        msgs, tools = _history(results=3), _tool_schemas()
        cb.record_usage("main", estimated=1000, actual=20000, msgs=msgs, tools=tools)
        self.assertLess(cb.prompt_tokens_for("main", msgs[:2], tools), 20000)

    def test_ratio_learns_from_successive_deltas(self):
        a = [{"role": "system", "content": "s" * 1000}]
        b = a + [{"role": "user", "content": "u" * 2000}]
        cb.record_usage("main", estimated=100, actual=500, msgs=a)
        cb.record_usage("main", estimated=200, actual=700, msgs=b)
        ratio = cb.snapshot()["main"]["chars_per_token"]
        self.assertGreater(ratio, cb.CHARS_PER_TOKEN_DEFAULT)
        self.assertLessEqual(ratio, cb.CHARS_PER_TOKEN_MAX)

    def test_unusable_counts_are_ignored(self):
        cb.record_usage("main", estimated=100, actual=0)
        cb.record_usage("main", estimated=100, actual=None)
        cb.record_usage("main", estimated="x", actual="y")
        self.assertEqual(cb.factor("main"), 1.0)
        self.assertIsNone(cb.snapshot()["main"]["last_prompt_tokens"])

    def test_reset_clears_learned_state(self):
        cb.record_usage("main", estimated=100, actual=900)
        cb.reset("main")
        self.assertEqual(cb.factor("main"), 1.0)
        self.assertIsNone(cb.snapshot()["main"]["last_prompt_tokens"])


class TestRootCause(unittest.TestCase):
    """The accounting error that shipped a ~36k prompt past a ~22.9k budget."""

    def setUp(self):
        cb.reset()

    def test_tool_blind_estimate_missed_the_budget(self):
        msgs, tools = _history(results=2), _tool_schemas(desc_chars=400)
        blind = estimate_prompt_tokens(msgs)                 # the old call: no tools
        aware = cb.prompt_tokens_for("main", msgs, tools)    # what the loop uses now
        budget = cb.budget_for("main", WINDOW_32K)
        self.assertLess(blind, budget, "history alone sat under the budget -> no compaction, 400")
        self.assertGreater(aware, budget, "counting the tool schemas must exceed the budget")
        self.assertGreater(aware - blind, 7000, "the uncounted tool block is thousands of tokens")

    def test_anchor_catches_what_the_estimate_understates(self):
        msgs, tools = _history(results=2), _tool_schemas(desc_chars=400)
        first = cb.prompt_tokens_for("main", msgs, tools)
        cb.record_usage("main", first, 36350, msgs, tools)     # the reported overflow
        after = cb.prompt_tokens_for("main", msgs, tools)
        self.assertGreaterEqual(after, 36350)
        self.assertGreater(after, first)

    def test_budget_tightens_after_a_real_overflow(self):
        msgs, tools = _history(), _tool_schemas()
        cb.record_usage("main", 22937, 36350, msgs, tools)
        self.assertLess(cb.budget_for("main", WINDOW_32K), BUDGET_32K)


class TestOverflowRecovery(unittest.TestCase):
    """routes/common.py: the 400-overflow path must be reachable, not shadowed.

    Before this change it was the LAST branch of the non-200 handler, so the
    grammar retry (executor default) and the JSON-repair retry raised first and
    the user saw `upstream 400: {...exceed_context_size_error...}` verbatim."""

    # the streaming request (llm_stream) and its overflow recovery (overflow)
    SRC_FILES = [Path(__file__).resolve().parents[1] / "routes" / "common" / f
                 for f in ("llm_stream.py", "overflow.py")]
    YOUR_ERROR = (b'{"error":{"code":400,"message":"request (36350 tokens) exceeds the available '
                  b'context size (32768 tokens), try increasing it","type":"exceed_context_size_error",'
                  b'"n_prompt_tokens":36350,"n_ctx":32768}}')

    def setUp(self):
        from routes import common
        self.common = common

    def test_reads_the_window_out_of_the_rejection(self):
        self.assertEqual(self.common._ctx_from_error(self.YOUR_ERROR), 32768)

    def test_junk_bodies_yield_zero(self):
        for body in (b"", b"<html>502 Bad Gateway</html>", b"[]", b'{"error":"text"}', None):
            with self.subTest(body=body):
                self.assertEqual(self.common._ctx_from_error(body), 0)

    def test_emergency_compaction_keeps_the_system_prompt_and_shrinks(self):
        msgs = _history(results=4)
        out = self.common._emergency_compact(msgs, _tool_schemas(), WINDOW_32K)
        self.assertEqual(out[0]["role"], "system")
        self.assertLess(len(out), len(msgs))
        self.assertLess(cb.prompt_chars(out), cb.prompt_chars(msgs))

    def test_emergency_compaction_without_a_window_still_works(self):
        msgs = _history(results=4)
        out = self.common._emergency_compact(msgs, None, 0)
        self.assertTrue(out)
        self.assertEqual(out[0]["role"], "system")
        self.assertLess(len(out), len(msgs))

    def test_overflow_branch_precedes_every_other_retry(self):
        src = "".join(f.read_text(encoding="utf-8") for f in self.SRC_FILES)
        handler = src.index("if response.status_code != 200:")
        overflow = src.index("_is_context_overflow(response.status_code, err_text)", handler)
        grammar = src.index("if grammar:", handler)
        repair = src.index('Failed to parse tool call arguments as JSON" in err_msg', handler)
        self.assertLess(overflow, grammar, "the grammar retry would raise before the overflow path")
        self.assertLess(overflow, repair, "the json-repair retry would raise before the overflow path")

    def test_every_attempt_routes_an_overflow_into_the_recovery(self):
        """The regression that let the 400 through: the first attempt was covered,
        but the grammar and tool-call-repair *retries* raised on their own. Every
        place that sends another attempt must delegate instead."""
        src = "".join(f.read_text(encoding="utf-8") for f in self.SRC_FILES)
        self.assertEqual(src.count("_recover_context("), 4,
                         "1 definition + 3 call sites (first attempt, grammar retry, repair retry)")
        for marker in ("if _is_context_overflow(response.status_code, err_text):",
                       "if _is_context_overflow(rg.status_code, rg_err):",
                       "if _is_context_overflow(retry_resp.status_code, re_err):"):
            with self.subTest(marker=marker):
                self.assertIn(marker, src)
        # and the recovery itself must never re-enter the streaming function (no loop)
        body = src[src.index("async def _recover_context("):src.index("def _ctx_from_error(")]
        self.assertNotIn("_llm_chat_stream_raw(", body)

    def test_overflow_branch_uses_the_window_it_was_told(self):
        src = "".join(f.read_text(encoding="utf-8") for f in self.SRC_FILES)
        body = src[src.index("def _recover_context("):src.index("def _ctx_from_error(")]
        self.assertIn("_emergency_compact(msgs, tools, _ctx_from_error(err_text))", body)
        self.assertIn('payload_notools.pop("tools", None)', body,
                      "the last-resort tier (plain chat) must stay wired")


class _SseOk:
    """A 200 SSE response the streaming layer can consume."""

    def __init__(self, text):
        self.status_code = 200
        self._lines = [
            "data: " + json.dumps({"choices": [{"delta": {"content": text}}]}),
            "data: " + json.dumps({"choices": [{"finish_reason": "stop", "delta": {}}]}),
            "data: [DONE]",
        ]

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return b""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _SseReject:
    """A 400 carrying llama-server's overflow body - the shape from the report."""

    def __init__(self, body, status=400):
        self.status_code, self._body = status, body

    async def aread(self):
        return self._body

    async def aiter_lines(self):
        return
        yield

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _ScriptedClient:
    """Replays a fixed list of responses and records every payload it was sent."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def stream(self, method, url, json=None, timeout=None):
        self.calls.append(json)
        return self._responses.pop(0)


class TestRecoveryEndToEnd(unittest.TestCase):
    """The reported failure driven through the real _llm_chat_stream_raw.

    Before this change the caller got
        RuntimeError: upstream 400: {"error": {..."exceed_context_size_error"...}}
    because the grammar retry (always set on the executor lane) raised first.
    """

    OVERFLOW = (b'{"error":{"code":400,"message":"request (36350 tokens) exceeds the available '
                b'context size (32768 tokens), try increasing it","type":"exceed_context_size_error",'
                b'"n_prompt_tokens":36350,"n_ctx":32768}}')

    def setUp(self):
        from routes import common
        self.common = common
        cb.reset()

    @staticmethod
    def _msgs():
        return ([{"role": "system", "content": "system prompt"},
                 {"role": "user", "content": "go"}]
                + [{"role": "tool", "tool_call_id": f"c{i}", "content": "x" * 20000} for i in range(3)])

    def _run(self, client, tools, **kw):
        async def go():
            out = []
            async for ev, val in self.common._llm_chat_stream_raw(
                    client, self._msgs(), tools, **kw):
                out.append((ev, val))
            return out
        return asyncio.run(go())

    def test_recovers_with_a_grammar_set(self):
        client = _ScriptedClient([_SseReject(self.OVERFLOW), _SseOk("recovered")])
        events = self._run(client, _tool_schemas(2, 80), grammar='root ::= "hi"')
        self.assertEqual("".join(v for ev, v in events if ev == "content_delta"), "recovered")
        self.assertEqual(len(client.calls), 2, "exactly one compacted retry")
        first, second = client.calls
        self.assertLess(cb.prompt_chars(second["messages"]), cb.prompt_chars(first["messages"]),
                        "the retry must actually be smaller than the rejected prompt")

    def test_recovers_without_a_grammar(self):
        client = _ScriptedClient([_SseReject(self.OVERFLOW), _SseOk("ok")])
        events = self._run(client, _tool_schemas(2, 80))
        self.assertEqual("".join(v for ev, v in events if ev == "content_delta"), "ok")

    def test_escalates_to_plain_chat_before_failing(self):
        """A second refusal escalates to the last resort: the same pruned prompt with
        the tool schemas dropped, so the user still gets an answer."""
        client = _ScriptedClient([_SseReject(self.OVERFLOW), _SseReject(self.OVERFLOW),
                                  _SseOk("recovered")])
        events = self._run(client, _tool_schemas(2, 80))
        self.assertEqual("".join(v for ev, v in events if ev == "content_delta"), "recovered")
        self.assertEqual(len(client.calls), 3)
        self.assertIn("tools", client.calls[1], "first retry keeps the tool schemas")
        self.assertNotIn("tools", client.calls[2], "last resort must drop them")

    def test_final_error_names_the_compaction(self):
        client = _ScriptedClient([_SseReject(self.OVERFLOW), _SseReject(self.OVERFLOW),
                                  _SseReject(self.OVERFLOW)])
        with self.assertRaises(RuntimeError) as ctx:
            self._run(client, _tool_schemas(2, 80))
        self.assertIn("after context compaction", str(ctx.exception))

    def test_final_error_without_tools(self):
        client = _ScriptedClient([_SseReject(self.OVERFLOW), _SseReject(self.OVERFLOW)])
        with self.assertRaises(RuntimeError) as ctx:
            self._run(client, None)
        self.assertIn("after context compaction", str(ctx.exception))
        self.assertEqual(len(client.calls), 2, "no plain-chat tier without tools to drop")

    # --- the two retry paths that used to bypass recovery entirely --------------
    # The reported failure kept coming back as a bare `upstream 400: {...}` because a
    # *retry* (not the first attempt) is what crossed the limit, and the retry sites
    # raised without compacting.

    def test_overflow_on_the_grammar_retry_recovers(self):
        client = _ScriptedClient([
            _SseReject(b'{"error":{"code":400,"message":"invalid grammar"}}'),   # 1st: grammar refused
            _SseReject(self.OVERFLOW),                                          # 2nd: now too big
            _SseOk("recovered"),
        ])
        events = self._run(client, _tool_schemas(2, 80), grammar='root ::= "hi"')
        self.assertEqual("".join(v for ev, v in events if ev == "content_delta"), "recovered")
        self.assertEqual(len(client.calls), 3)
        self.assertNotIn("grammar", client.calls[1], "the retry drops the grammar")
        self.assertLess(cb.prompt_chars(client.calls[2]["messages"]),
                        cb.prompt_chars(client.calls[1]["messages"]),
                        "the recovery must send a pruned prompt")

    def test_overflow_on_the_tool_call_repair_retry_recovers(self):
        client = _ScriptedClient([
            _SseReject(b'{"error":{"message":"Failed to parse tool call arguments as JSON"}}', status=500),
            _SseReject(self.OVERFLOW),
            _SseOk("recovered"),
        ])
        events = self._run(client, _tool_schemas(2, 80))
        self.assertEqual("".join(v for ev, v in events if ev == "content_delta"), "recovered")
        self.assertEqual(len(client.calls), 3)
        self.assertIn("tools", client.calls[2], "the repair retry keeps its tools")

    def test_retry_path_still_raises_the_plain_error_for_other_failures(self):
        """Non-overflow retry failures must keep their original diagnostics."""
        client = _ScriptedClient([
            _SseReject(b'{"error":{"code":400,"message":"invalid grammar"}}'),
            _SseReject(b'<html>502 Bad Gateway</html>', status=502),
        ])
        with self.assertRaises(RuntimeError) as ctx:
            self._run(client, _tool_schemas(2, 80), grammar='root ::= "hi"')
        self.assertNotIn("after context compaction", str(ctx.exception))
        self.assertIn("502", str(ctx.exception))

    def test_overflow_detection_needs_both_status_and_text(self):
        common = self.common
        self.assertTrue(common._is_context_overflow(400, self.OVERFLOW))
        self.assertTrue(common._is_context_overflow(400, b"request exceeds the available context size (x)"))
        self.assertFalse(common._is_context_overflow(500, self.OVERFLOW),
                         "a 500 carrying the text is not the overflow branch")
        self.assertFalse(common._is_context_overflow(400, b"invalid grammar"))
        self.assertFalse(common._is_context_overflow(None, b""))
        self.assertFalse(common._is_context_overflow("weird", self.OVERFLOW))
