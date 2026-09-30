"""History shaping in core/agent_loop.py::compact_messages.

Two Phase-3 changes, both inside the mechanical (no-LLM) compaction path:

  * an aging bound - at most `context.aging.keep_recent_results` tool results stay
    verbatim, so one heavy step cannot dominate every later request. MAX_TOOL_OUTPUT
    is 20,000 chars (~6.7k tokens), so a "60% of the budget" tail could otherwise be
    three or four max-size results;
  * the digest is folded into the first digested user turn instead of being inserted
    as a new system message at index 1, which keeps the system-prompt + tool-schema
    prefix byte-identical for llama.cpp's KV cache (--cache-reuse).

API validity is the invariant that matters most here: an assistant tool_call without
its tool result, or a tool result without its call, makes the next request malformed.

Run: python -m unittest tests.test_context_aging -v
"""

import copy
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.agent_loop as al  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def _history(results=8, size=20000, parallel_last=1, last_size=None):
    """A long run: big system prompt, one request, then `results` max-size tool
    results, ending in a final assistant turn. `last_size` overrides the size of the
    final (parallel) turn, to model a small newest turn after heavy earlier ones."""
    msgs = [{"role": "system", "content": "S" * 2000},
            {"role": "user", "content": "build the thing"}]
    for i in range(results - 1):
        msgs.append({"role": "assistant", "content": "",
                     "tool_calls": [{"id": f"c{i}", "type": "function",
                                     "function": {"name": "read_file",
                                                  "arguments": json.dumps({"path": f"f{i}.py"})}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "x" * size})
    tcs = [{"id": f"z{k}", "type": "function",
            "function": {"name": "grep", "arguments": "{}"}} for k in range(parallel_last)]
    msgs.append({"role": "assistant", "content": "", "tool_calls": tcs})
    for k in range(parallel_last):
        msgs.append({"role": "tool", "tool_call_id": f"z{k}",
                     "content": "y" * (size if last_size is None else last_size)})
    msgs.append({"role": "assistant", "content": "done"})
    return msgs


def _roles(msgs):
    return "".join({"system": "S", "user": "U", "assistant": "A", "tool": "t"}
                   .get(m.get("role"), "?") for m in msgs)


def _orphans(msgs):
    """tool_call ids without a tool result, plus tool results without a call."""
    calls = {tc.get("id") for m in msgs if m.get("role") == "assistant"
             for tc in (m.get("tool_calls") or [])}
    results = {m.get("tool_call_id") for m in msgs if m.get("role") == "tool"}
    return calls ^ results


def _compact(msgs, budget, keep):
    with mock.patch.object(al.compaction, "_keep_recent_results", return_value=keep):
        return al.compact_messages(copy.deepcopy(msgs), budget)


class TestAgingBound(unittest.TestCase):

    def setUp(self):
        self.msgs = _history()

    def test_bound_is_honoured(self):
        for keep, expected in ((1, 1), (2, 2), (3, 3)):
            with self.subTest(keep=keep):
                out = _compact(self.msgs, 40000, keep)
                self.assertEqual(sum(1 for m in out if m.get("role") == "tool"), expected)

    def test_zero_disables_the_bound(self):
        """0 keeps the historic "60% of the budget" tail."""
        out = _compact(self.msgs, 40000, 0)
        self.assertEqual(sum(1 for m in out if m.get("role") == "tool"), 3)

    def test_bound_actually_shrinks_the_prompt(self):
        sizes = {k: al.estimate_prompt_tokens(_compact(self.msgs, 40000, k)) for k in (0, 1, 2)}
        self.assertLess(sizes[2], sizes[0])
        self.assertLess(sizes[1], sizes[2])

    def test_no_orphaned_tool_traffic(self):
        for keep in (0, 1, 2, 3):
            with self.subTest(keep=keep):
                self.assertEqual(_orphans(_compact(self.msgs, 40000, keep)), set())

    def test_system_prompt_is_never_touched(self):
        for keep in (0, 2):
            with self.subTest(keep=keep):
                out = _compact(self.msgs, 40000, keep)
                self.assertEqual(out[0], self.msgs[0])
                self.assertEqual(out[0]["role"], "system")

    def test_under_budget_history_is_returned_untouched(self):
        self.assertIs(al.compact_messages(self.msgs, 999999), self.msgs)

    def test_keeps_working_across_budgets(self):
        for budget in (45000, 40000, 30000, 20000, 8000):
            with self.subTest(budget=budget):
                out = _compact(self.msgs, budget, 2)
                self.assertTrue(out, "compaction must never return nothing")
                self.assertEqual(out[0]["role"], "system")
                self.assertEqual(_orphans(out), set())


class TestParallelNewestTurn(unittest.TestCase):
    """Characterization, not a goal: a final turn with several parallel max-size
    results is larger than the 60% tail budget, so those results are digested today
    (pre-existing behaviour of the tail budget, unchanged by the aging bound). The
    only hard requirement is that the prompt stays API-valid."""

    def test_parallel_results_do_not_break_the_prompt(self):
        msgs = _history(results=6, parallel_last=4)
        for keep in (1, 2):
            with self.subTest(keep=keep):
                out = _compact(msgs, 40000, keep)
                self.assertEqual(_orphans(out), set())
                self.assertTrue(_roles(out).startswith("SU"))

    def test_the_newest_unit_is_never_cut_by_the_bound(self):
        """A small parallel newest turn survives verbatim even when it holds more
        results than the bound allows - it is what the model is answering right now."""
        msgs = _history(results=8, size=20000, parallel_last=3, last_size=200)
        self.assertGreater(al.estimate_prompt_tokens(msgs), 40000, "compaction must be needed")
        out = _compact(msgs, 40000, 1)
        self.assertEqual(sum(1 for m in out if m.get("role") == "tool"), 3)
        self.assertEqual(_orphans(out), set())


class TestDigestPlacement(unittest.TestCase):

    def setUp(self):
        self.msgs = _history()

    def test_digest_lives_in_a_user_turn_not_a_new_system_message(self):
        out = _compact(self.msgs, 40000, 2)
        self.assertEqual(out[1]["role"], "user", "index 1 must not be a synthetic system message")
        self.assertIn("CONVERSATION DIGEST", out[1]["content"])
        self.assertEqual(sum(1 for m in out if m.get("role") == "system"), 1)

    def test_the_original_request_survives_under_the_digest(self):
        out = _compact(self.msgs, 40000, 2)
        self.assertIn("build the thing", out[1]["content"])

    def test_fallback_shape_when_no_user_turn_was_compacted_away(self):
        msgs = [{"role": "system", "content": "S" * 2000},
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "a", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "a", "content": "x" * 20000},
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "b", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "b", "content": "x" * 20000}]
        out = al.compact_messages(copy.deepcopy(msgs), 6000)
        if len(out) > 1 and out[1].get("role") == "system":
            self.assertIn("CONVERSATION DIGEST", out[1]["content"])
        self.assertEqual(out[0]["role"], "system")
        self.assertEqual(_orphans(out), set())


class TestKeepRecentConfig(unittest.TestCase):

    def test_default_comes_from_config(self):
        self.assertEqual(al._keep_recent_results(), 6)

    def test_zero_and_junk_are_handled(self):
        for cfg, expected in (({"keep_recent_results": 0}, 0),
                              ({"keep_recent_results": 5}, 5),
                              ({}, 6),
                              ({"keep_recent_results": "nonsense"}, 6)):
            with mock.patch.dict("core.small_model.APP_CONFIG",
                                 {"context": {"aging": cfg}}, clear=False):
                with self.subTest(cfg=cfg):
                    self.assertEqual(al._keep_recent_results(), expected)

    def test_missing_config_block_uses_the_default(self):
        with mock.patch.dict("core.small_model.APP_CONFIG", {}, clear=True):
            self.assertEqual(al._keep_recent_results(), 6)


class TestReadFilePagingHint(unittest.TestCase):
    """A truncated result must tell the model how to get the rest, otherwise the
    truncation is silent information loss."""

    def test_both_branches_advertise_read_file_chunk(self):
        src = (REPO / "core" / "agent_tools" / "file_ops.py").read_text(encoding="utf-8")
        start = src.index("async def tool_read_file(")
        nxt = [i for i in (src.find("\nasync def ", start + 1), src.find("\ndef ", start + 1)) if i > 0]
        block = src[start:min(nxt) if nxt else len(src)]
        self.assertEqual(block.count("read_file_chunk("), 2,
                         "the remote and local truncation branches both need the hint")
        self.assertIn("offset_chars={MAX_TOOL_OUTPUT}", block)


if __name__ == "__main__":
    unittest.main()
