"""tests/test_guard_semantic.py - Semantic (natural-language) rules, null
tolerance, and the /agent/run pre-stream regression.

Run: python -m unittest tests.test_guard_semantic -v
"""

import asyncio
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import input_guard, output_guard


class FakePrincipal:
    def __init__(self, username="alice", roles=("user",)):
        self.id = 1
        self.username = username
        self.role_names = list(roles)


def set_rules(cfg_key, rules, enabled=True):
    from core import small_model
    small_model.APP_CONFIG[cfg_key] = {"enabled": enabled, "rules": rules}
    input_guard._compile_cache.clear()
    input_guard._cache_ts = 0.0


SEM_RULE = {
    "id": "s1", "name": "Transactional data", "type": "semantic",
    "description": "Any content containing transactional data (payments, invoices, transfers)",
    "scope": "cloud_only", "roles": [], "users": [],
    "message": "Transactional data may not be sent to cloud models.",
}


def _make_async(val):
    async def _f(policy, text):
        return val
    return _f          # return the async fn itself; callers await _classify(...)


class TestSemanticInput(unittest.TestCase):
    def tearDown(self):
        set_rules("input_guard", [])

    def _run(self, coro):
        return asyncio.new_event_loop().run_until_complete(coro)

    def test_semantic_rule_blocks_via_classifier(self):
        set_rules("input_guard", [SEM_RULE])
        input_guard._classify = _make_async(True)
        hit = self._run(input_guard.check_async(
            ["pay invoice INV-1"], FakePrincipal(), True))
        self.assertIsNotNone(hit)
        self.assertEqual(hit["message"], "Transactional data may not be sent to cloud models.")

    def test_semantic_rule_passes_when_classifier_says_no(self):
        set_rules("input_guard", [SEM_RULE])
        input_guard._classify = _make_async(False)
        hit = self._run(input_guard.check_async(
            ["hello world"], FakePrincipal(), True))
        self.assertIsNone(hit)

    def test_semantic_cloud_only_skipped_for_local(self):
        set_rules("input_guard", [SEM_RULE])
        called = []

        def fake(policy, text):
            called.append(1)
            return _make_async(True)

        input_guard._classify = fake
        hit = self._run(input_guard.check_async(
            ["pay invoice INV-1"], FakePrincipal(), False))
        self.assertIsNone(hit)
        self.assertEqual(called, [])

    def test_regex_rules_still_win_without_classifier(self):
        set_rules("input_guard", [
            SEM_RULE,
            {"id": "r1", "name": "No invoices", "patterns": [r"\bINV-\d+"],
             "scope": "block_all", "roles": [], "users": [], "message": "blocked"},
        ])
        # classifier not patched: must not be called since regex hits first
        hit = self._run(input_guard.check_async(
            ["see invoice INV-77"], FakePrincipal(), False))
        self.assertIsNotNone(hit)
        self.assertEqual(hit["name"], "No invoices")

    def test_semantic_rule_ignored_by_sync_check(self):
        set_rules("input_guard", [SEM_RULE])
        # sync check() must skip semantic rules entirely (no classifier needed)
        self.assertIsNone(input_guard.check(["invoice INV-1"], FakePrincipal(), True))


class TestSemanticOutput(unittest.TestCase):
    def tearDown(self):
        set_rules("output_guard", [])

    def _run(self, coro):
        return asyncio.new_event_loop().run_until_complete(coro)

    def test_semantic_output_hit_and_scope(self):
        set_rules("output_guard", [SEM_RULE])
        input_guard._classify = _make_async(True)
        hit = self._run(output_guard.semantic_check(
            "here is the transaction: paid $500", FakePrincipal(), True))
        self.assertIsNotNone(hit)
        hit2 = self._run(output_guard.semantic_check(
            "here is the transaction: paid $500", FakePrincipal(), False))
        self.assertIsNone(hit2)

    def test_semantic_output_no_match(self):
        set_rules("output_guard", [SEM_RULE])
        input_guard._classify = _make_async(False)
        hit = self._run(output_guard.semantic_check(
            "the weather is nice", FakePrincipal(), True))
        self.assertIsNone(hit)


class TestNullTolerance(unittest.TestCase):
    def test_null_strings_mean_no_restriction(self):
        rule = dict(SEM_RULE, roles=["null", "user"], users=["all", ""])
        self.assertTrue(input_guard._rule_applies_to(rule, FakePrincipal(username="bob")))

    def test_validate_rules_coerces_null(self):
        clean, problems = input_guard.validate_rules([
            {"name": "r", "type": "regex", "patterns": ["abc"], "scope": "block_all",
             "roles": ["null"], "users": ["all"]},
        ])
        self.assertEqual(len(clean), 1)
        self.assertEqual(clean[0]["roles"], [])
        self.assertEqual(clean[0]["users"], [])
        self.assertEqual(problems, [])

    def test_validate_rules_semantic(self):
        clean, problems = input_guard.validate_rules([
            {"name": "ok", "type": "semantic", "description": "PII", "scope": "block_all",
             "roles": "null", "users": None},
            {"name": "bad", "type": "semantic", "description": "", "scope": "block_all"},
            {"name": "badtype", "type": "weird", "patterns": ["x"], "scope": "block_all"},
        ])
        self.assertEqual(len(clean), 1)
        self.assertEqual(clean[0]["type"], "semantic")
        self.assertEqual(clean[0]["description"], "PII")
        self.assertEqual(len(problems), 2)


class TestAgentRunRegression(unittest.TestCase):
    """The input-guard hook must run AFTER `msgs` is assigned in agent_run —
    referencing it earlier crashed /agent/run with a 500 (UnboundLocalError)."""

    def test_guard_hook_after_msgs_assignment(self):
        src = (Path(__file__).resolve().parents[1] / "routes" / "agent.py").read_text(
            encoding="utf-8")
        msgs_line = src.find("msgs = [dict(m) for m in req.messages]")
        hook_line = src.find("input_guard.check_async")
        self.assertGreater(msgs_line, 0, "msgs assignment missing")
        self.assertGreater(hook_line, msgs_line,
                           "input-guard hook must come after the msgs assignment")

    def test_no_duplicate_knowledge_import(self):
        src = (Path(__file__).resolve().parents[1] / "routes" / "agent.py").read_text(
            encoding="utf-8")
        self.assertEqual(
            len(re.findall(r"from core\.knowledge_access import allowed_source_ids_for", src)),
            1)


if __name__ == "__main__":
    unittest.main()

