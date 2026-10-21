"""tests/test_context_snip.py - the narration snip layer (core/agent_loop/snip.py).

clearing.py turns old *tool results* into placeholders; compaction.py is the
last resort. What nothing touched is old *assistant narration*: the prose an
assistant message carries (beside or without tool_calls) stays verbatim until
the whole prompt is compacted. snip.py is that middle layer: stub old long
narration, keep the recent tail, never touch tool results / user turns /
the system prompt, and never rewrite for a small gain (same cache discipline
as clearing). It is DEFAULT-OFF (context.snip.enabled): merged flag-off, the
byte-for-byte guarantee this file pins first.

Run: python -m unittest tests.test_context_snip -v
"""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.agent_loop import snip as S


def narrate(n_narrations, chars=3000, with_tools_for=0):
    """system, user, then n assistant messages. The first `with_tools_for` also
    carry a tool_call (prose beside actions is the common shape)."""
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "do it"}]
    for i in range(n_narrations):
        m = {"role": "assistant", "content": f"A{i}-" + "n" * chars}
        if i < with_tools_for:
            m["tool_calls"] = [{"id": f"t{i}", "type": "function",
                                "function": {"name": "read_file", "arguments": json.dumps({"path": f"f{i}.js"})}}]
        msgs.append(m)
    return msgs


CFG = {"snip": {"enabled": True, "trigger_tokens": 1000, "keep_recent_narration": 3,
                "min_chars": 600, "at_least_tokens": 500}}


def with_cfg(cfg):
    return mock.patch.dict("core.small_model.APP_CONFIG", {"context": cfg}, clear=False)


class NoOpGuarantee(unittest.TestCase):
    """The default state must be byte-for-byte identical to not calling snip."""

    def test_default_flag_off_is_a_strict_no_op(self):
        msgs = narrate(6)
        before = json.dumps(msgs)
        with with_cfg({}):                                   # no context.snip key at all
            self.assertEqual(S.snip_old_narration(msgs, 10 ** 6), 0)
        self.assertEqual(json.dumps(msgs), before)

    def test_disabled_with_false_stays_a_no_op(self):
        msgs = narrate(6)
        before = json.dumps(msgs)
        with with_cfg({"snip": {"enabled": False, "trigger_tokens": 1000}}):
            self.assertEqual(S.snip_old_narration(msgs, 10 ** 6), 0)
        self.assertEqual(json.dumps(msgs), before)

    def test_kill_flag_survives_a_broken_config(self):
        msgs = narrate(6)
        before = json.dumps(msgs)
        with with_cfg({"snip": {"enabled": "whatever-not-true"}}):
            self.assertEqual(S.snip_old_narration(msgs, 10 ** 6), 0)
        self.assertEqual(json.dumps(msgs), before)


class Snipping(unittest.TestCase):
    def test_old_narration_is_stubbed_and_the_recent_tail_stays(self):
        msgs = narrate(6)
        with with_cfg(CFG):
            freed = S.snip_old_narration(msgs, 50000)
        self.assertGreater(freed, 0)
        snipped = [m for m in msgs if m["role"] == "assistant" and m["content"].startswith(S.SNIP_PREFIX)]
        self.assertEqual(len(snipped), 3)                    # the oldest 3
        # every assistant message is either a stub or the verbatim original
        self.assertTrue(all(m["content"].startswith(S.SNIP_PREFIX) or m["content"][:2] == f"A{i}"
                            for i, m in enumerate((m for m in msgs if m["role"] == "assistant"))))
        kept = [m for m in msgs if m["role"] == "assistant" and not m["content"].startswith(S.SNIP_PREFIX)]
        self.assertEqual([m["content"][:2] for m in kept], ["A3", "A4", "A5"])   # newest 3 verbatim
        self.assertIn("narration", snipped[0]["content"])
        self.assertLess(len(snipped[0]["content"]), 400)

    def test_system_user_and_tool_roles_are_never_touched(self):
        msgs = [{"role": "system", "content": "sys - " + "s" * 4000},
                {"role": "user", "content": "user - " + "u" * 4000}]
        msgs += narrate(5)[2:]
        msgs.append({"role": "user", "content": "follow-up - " + "f" * 4000})
        msgs.append({"role": "tool", "tool_call_id": "t", "content": "R - " + "r" * 4000})
        with with_cfg(CFG):
            S.snip_old_narration(msgs, 50000)
        self.assertEqual(msgs[0]["content"], "sys - " + "s" * 4000)
        self.assertEqual(msgs[1]["content"], "user - " + "u" * 4000)
        self.assertEqual(msgs[-2]["content"], "follow-up - " + "f" * 4000)
        self.assertEqual(msgs[-1]["content"], "R - " + "r" * 4000)
        self.assertEqual([m["role"] for m in msgs].count("tool"), 1)

    def test_tool_calls_and_pairing_survive_a_snipped_message(self):
        msgs = narrate(6, with_tools_for=6)
        ids = [tc["id"] for m in msgs for tc in (m.get("tool_calls") or [])]
        with with_cfg(CFG):
            S.snip_old_narration(msgs, 50000)
        self.assertEqual(ids, [tc["id"] for m in msgs for tc in (m.get("tool_calls") or [])])
        old = msgs[2]
        self.assertTrue(old["content"].startswith(S.SNIP_PREFIX))
        self.assertEqual(len(old["tool_calls"]), 1)          # the action record stays

    def test_below_the_trigger_nothing_happens(self):
        msgs = narrate(6)
        before = json.dumps(msgs)
        with with_cfg(CFG):
            self.assertEqual(S.snip_old_narration(msgs, 900), 0)
        self.assertEqual(json.dumps(msgs), before)

    def test_small_gains_are_not_worth_rewriting_the_cache(self):
        msgs = narrate(6, chars=650)                          # 3 snippable x ~162 tokens < at_least 500
        before = json.dumps(msgs)
        with with_cfg(CFG):
            self.assertEqual(S.snip_old_narration(msgs, 50000), 0)
        self.assertEqual(json.dumps(msgs), before)

    def test_short_narrations_are_not_worth_a_placeholder(self):
        msgs = narrate(6, chars=500)                          # under min_chars even when old
        before = json.dumps(msgs)
        with with_cfg(CFG):
            self.assertEqual(S.snip_old_narration(msgs, 10 ** 6), 0)
        self.assertEqual(json.dumps(msgs), before)

    def test_idempotent(self):
        msgs = narrate(6)
        with with_cfg(CFG):
            S.snip_old_narration(msgs, 50000)
            once = json.dumps(msgs)
            self.assertEqual(S.snip_old_narration(msgs, 50000), 0)   # already snipped: nothing left
        self.assertEqual(json.dumps(msgs), once)

    def test_squeeze_fires_harder(self):
        msgs = narrate(6)
        with with_cfg({**CFG, "snip": {**CFG["snip"], "trigger_tokens": 90000}}):
            self.assertEqual(S.snip_old_narration(msgs, 40000), 0)     # under the normal trigger
            self.assertGreater(S.snip_old_narration(msgs, 40000, squeeze=True), 0)
        kept = [m for m in msgs if m["role"] == "assistant" and not m["content"].startswith(S.SNIP_PREFIX)]
        self.assertLessEqual(len(kept), 2)                    # keep // 2

    def test_disabled_with_zero_trigger(self):
        msgs = narrate(6)
        with with_cfg({"snip": {"enabled": True, "trigger_tokens": 0}}):
            self.assertEqual(S.snip_old_narration(msgs, 10 ** 6), 0)

    def test_more_recent_than_squeeze_keep_is_still_never_touched(self):
        # even in squeeze mode at least one assistant message must stay verbatim
        msgs = narrate(6)
        with with_cfg(CFG):
            S.snip_old_narration(msgs, 10 ** 6, squeeze=True)
        last = [m for m in msgs if m["role"] == "assistant"][-1]
        self.assertFalse(last["content"].startswith(S.SNIP_PREFIX))


class RunWiring(unittest.TestCase):
    # mirrored from tests/test_clearing.py: the loop body is scanned so a
    # regression that removes the wiring still fails even if snip.py is green
    src = Path("routes/agent/stream.py").read_text(encoding="utf-8")

    def test_snip_is_wired_after_clearing(self):
        self.assertIn("from core.agent_loop.snip import snip_old_narration", self.src)
        self.assertLess(self.src.index("clear_old_results(msgs, pre_tokens"),
                        self.src.index("snip_old_narration(msgs, pre_tokens"))
        self.assertIn("route_log.event(run_id, step, q_category, lane_name, \"snip\"", self.src)
        self.assertIn("context.snip.enabled", self.src)      # the default-off marker is visible at the call site


class DeferredSchemasPremise(unittest.TestCase):
    """C2 (deferred MCP tool schemas) was scoped out, not built: prompt_scope
    already hides unmentioned-MCP schemas (S1) and tool_surface hides situational
    families. The remaining population (MANY schemas across mentioned servers)
    needs traffic evidence first - this pins the wiring that makes the premise
    checkable."""

    def test_s1_and_families_already_bound_mcp_and_family_schema_cost(self):
        src = "\n".join(p.read_text(encoding="utf-8")
                        for p in (Path("routes/agent/stream.py"), Path("core/prompt_scope.py"),
                                  Path("core/tool_surface.py")))
        self.assertIn("prompt_scope.hide_unmentioned_mcp(", src)
        self.assertIn("def hidden_families", src)


if __name__ == "__main__":
    unittest.main()
