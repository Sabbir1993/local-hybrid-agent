"""tests/test_clearing.py - tool-result clearing, the cloud context cap, the run token budget wiring.

Run: python -m unittest tests.test_clearing -v
"""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import context_budget
from core.agent_loop import clearing as C


def run(n_tools, size=4000, name="read_file"):
    """system, user, then n (assistant tool_call + tool result) pairs."""
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "do it"}]
    for i in range(n_tools):
        msgs.append({"role": "assistant", "content": "",
                     "tool_calls": [{"id": f"t{i}", "type": "function",
                                     "function": {"name": name, "arguments": json.dumps({"path": f"f{i}.js"})}}]})
        msgs.append({"role": "tool", "tool_call_id": f"t{i}", "content": f"R{i}-" + "x" * size})
    return msgs


CFG = {"clear_trigger_tokens": 1000, "clear_keep_results": 4, "clear_at_least_tokens": 500}


def with_cfg(cfg):
    return mock.patch.dict("core.small_model.APP_CONFIG", {"context": cfg}, clear=False)


class Clearing(unittest.TestCase):
    def test_old_results_become_placeholders_and_the_newest_stay(self):
        msgs = run(10)
        with with_cfg(CFG):
            freed = C.clear_old_results(msgs, 50000)
        tools = [m for m in msgs if m["role"] == "tool"]
        self.assertGreater(freed, 0)
        self.assertTrue(all(t["content"].startswith(C.CLEARED_PREFIX) for t in tools[:6]))
        self.assertTrue(all(t["content"].startswith("R") for t in tools[6:]))        # the newest 4 untouched
        self.assertIn("read_file(f0.js)", tools[0]["content"])                        # says what it was
        self.assertLess(len(tools[0]["content"]), 200)

    def test_tool_call_records_and_pairing_survive(self):
        msgs = run(8)
        ids = [tc["id"] for m in msgs for tc in (m.get("tool_calls") or [])]
        with with_cfg(CFG):
            C.clear_old_results(msgs, 50000)
        self.assertEqual(ids, [tc["id"] for m in msgs for tc in (m.get("tool_calls") or [])])
        self.assertEqual([m["tool_call_id"] for m in msgs if m["role"] == "tool"], ids)

    def test_below_the_trigger_nothing_happens(self):
        msgs = run(10)
        before = json.dumps(msgs)
        with with_cfg(CFG):
            self.assertEqual(C.clear_old_results(msgs, 900), 0)
        self.assertEqual(json.dumps(msgs), before)

    def test_small_gains_are_not_worth_rewriting_the_cache(self):
        msgs = run(6, size=700)                      # clearable: 2 x ~175 tokens < at_least 500
        before = json.dumps(msgs)
        with with_cfg(CFG):
            self.assertEqual(C.clear_old_results(msgs, 50000), 0)
        self.assertEqual(json.dumps(msgs), before)

    def test_idempotent_and_batched(self):
        msgs = run(10)
        with with_cfg(CFG):
            C.clear_old_results(msgs, 50000)
            once = json.dumps(msgs)
            self.assertEqual(C.clear_old_results(msgs, 50000), 0)     # nothing left to clear
        self.assertEqual(json.dumps(msgs), once)

    def test_skill_and_plan_results_are_never_cleared(self):
        msgs = run(10, name="read_skill")
        with with_cfg(CFG):
            self.assertEqual(C.clear_old_results(msgs, 50000), 0)
        msgs = run(10, name="get_plan")
        with with_cfg(CFG):
            self.assertEqual(C.clear_old_results(msgs, 50000), 0)

    def test_squeeze_clears_harder(self):
        msgs = run(10)
        with with_cfg({**CFG, "clear_trigger_tokens": 90000}):
            self.assertEqual(C.clear_old_results(msgs, 40000), 0)             # under the normal trigger
            self.assertGreater(C.clear_old_results(msgs, 40000, squeeze=True), 0)
        self.assertEqual(sum(1 for m in msgs if m["role"] == "tool" and m["content"].startswith("R")), 2)

    def test_disabled_with_zero_trigger(self):
        msgs = run(10)
        with with_cfg({**CFG, "clear_trigger_tokens": 0}):
            self.assertEqual(C.clear_old_results(msgs, 10 ** 6), 0)


class CloudCap(unittest.TestCase):
    def test_cloud_lanes_are_capped_local_ones_are_not(self):
        with with_cfg({"cloud_budget_tokens": 60000}):
            self.assertEqual(context_budget.budget_for("main", 262144, cloud=True), 60000)
            self.assertGreater(context_budget.budget_for("main", 262144, cloud=False), 100000)
            self.assertLess(context_budget.budget_for("main", 32768, cloud=True), 60000)     # a small window stays smaller

    def test_zero_means_no_cap(self):
        with with_cfg({"cloud_budget_tokens": 0}):
            self.assertGreater(context_budget.budget_for("main", 262144, cloud=True), 100000)


class RunWiring(unittest.TestCase):
    # the loop body lives in stream.py since 2026-10-06; scan both modules so a
    # regression that removes the wiring from either file still fails
    src = "\n".join(p.read_text(encoding="utf-8")
                    for p in (Path("routes/agent/run.py"),
                              Path("routes/agent/stream.py")))

    def test_budget_and_clearing_are_wired(self):
        for needle in ("clear_old_results(msgs, pre_tokens, squeeze=budget_squeeze)", 'stop_reason = "budget"',
                       "run_token_budget", "cloud=bool(cloud_main)", "[budget]"):
            self.assertIn(needle, self.src, needle)

    def test_a_budget_stop_is_a_stop_with_a_summary(self):
        self.assertIn('"budget"', self.src[self.src.index("stop_reason in ("):][:120])


if __name__ == "__main__":
    unittest.main()


class ReviseStaysSmall(unittest.TestCase):
    def test_revision_prompt_is_system_request_draft_issues_only(self):
        from core.verifier.engine import revision_messages
        msgs = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "old question " * 500},
                {"role": "assistant", "content": "old answer " * 500}, {"role": "user", "content": "real question"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]}, {"role": "tool", "content": "T" * 9000},
                {"role": "user", "content": "[continue] keep going"}]
        out = revision_messages(msgs, "DRAFT", [{"severity": "major", "text": "wrong"}])
        self.assertEqual([m["role"] for m in out], ["system", "user", "assistant", "user"])
        self.assertEqual(out[1]["content"], "real question")            # not the old one, not the [continue] turn
        self.assertEqual(out[2]["content"], "DRAFT")
        self.assertIn("wrong", out[3]["content"])
        self.assertLess(sum(len(m["content"]) for m in out), 2000)


class McpAndSubagentStaySmall(unittest.TestCase):
    def test_base64_content_from_an_mcp_tool_is_a_placeholder(self):
        from core.mcp.constants import _stringify_content
        out = _stringify_content({"content": [{"type": "image", "data": "A" * 50000, "mimeType": "image/png"},
                                              {"type": "text", "text": "caption"}]})
        self.assertIn("image content omitted", out)
        self.assertIn("caption", out)
        self.assertLess(len(out), 200)
        self.assertIn('"uri"', _stringify_content({"content": [{"type": "resource", "uri": "x"}]}))   # small non-text stays

    def test_subagent_tool_results_are_capped(self):
        src = Path("core/subagent/runner.py").read_text(encoding="utf-8")
        self.assertIn('"content": cap_tool_result(result)', src)
