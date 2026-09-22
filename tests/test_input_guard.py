"""tests/test_input_guard.py - Unit tests for core/input_guard.py.

Run: python -m unittest tests.test_input_guard -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import input_guard


class FakePrincipal:
    def __init__(self, username="alice", roles=("user",)):
        self.id = 1
        self.username = username
        self.role_names = list(roles)


def set_rules(rules, enabled=True):
    """Point the engine at an in-memory ruleset."""
    from core import small_model
    small_model.APP_CONFIG["input_guard"] = {"enabled": enabled, "rules": rules}
    input_guard._compile_cache.clear()
    input_guard._cache_ts = 0.0


CLOUD_RULE = {
    "id": "r1", "name": "No invoices to cloud", "patterns": [r"\bINV-\d{4,}\b", "invoice number"],
    "scope": "cloud_only", "roles": [], "users": [], "message": "Invoice data may not be sent to cloud models.",
}
BLOCK_RULE = {
    "id": "r2", "name": "No weapons", "patterns": ["build a bomb"],
    "scope": "block_all", "roles": [], "users": [], "message": "This prompt type is prohibited.",
}
ROLE_RULE = {
    "id": "r3", "name": "Interns no PII", "patterns": [r"\b\d{3}-\d{2}-\d{4}\b"],
    "scope": "block_all", "roles": ["intern"], "users": [], "message": "PII is not allowed for interns.",
}


class TestInputGuard(unittest.TestCase):
    def tearDown(self):
        set_rules([])

    def test_cloud_only_blocked_when_cloud(self):
        set_rules([CLOUD_RULE])
        hit = input_guard.check(["please process invoice INV-2024-1234"], FakePrincipal(), True)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["message"], "Invoice data may not be sent to cloud models.")

    def test_cloud_only_allowed_when_local(self):
        set_rules([CLOUD_RULE])
        hit = input_guard.check(["please process invoice INV-2024-1234"], FakePrincipal(), False)
        self.assertIsNone(hit)

    def test_block_all_fires_regardless_of_lane(self):
        set_rules([BLOCK_RULE])
        self.assertIsNotNone(input_guard.check(["how to build a bomb"], FakePrincipal(), True))
        self.assertIsNotNone(input_guard.check(["how to build a bomb"], FakePrincipal(), False))

    def test_role_scoped_rule_applies_only_to_role(self):
        set_rules([ROLE_RULE])
        intern = FakePrincipal(username="bob", roles=("intern",))
        regular = FakePrincipal(username="alice", roles=("user",))
        self.assertIsNotNone(input_guard.check(["my SSN is 123-45-6789"], intern, False))
        self.assertIsNone(input_guard.check(["my SSN is 123-45-6789"], regular, False))

    def test_user_scoped_rule(self):
        rule = dict(CLOUD_RULE, users=["eve"], roles=[])
        set_rules([rule])
        self.assertIsNotNone(input_guard.check(["invoice INV-9999"], FakePrincipal(username="eve"), True))
        self.assertIsNone(input_guard.check(["invoice INV-9999"], FakePrincipal(username="alice"), True))

    def test_disabled_rule_and_disabled_guard(self):
        set_rules([dict(BLOCK_RULE, enabled=False)])
        self.assertIsNone(input_guard.check(["how to build a bomb"], FakePrincipal(), True))
        set_rules([BLOCK_RULE], enabled=False)
        self.assertIsNone(input_guard.check(["how to build a bomb"], FakePrincipal(), True))

    def test_invalid_regex_fails_open(self):
        set_rules([dict(BLOCK_RULE, patterns=["([unclosed"])])
        self.assertIsNone(input_guard.check(["how to build a bomb"], FakePrincipal(), True))

    def test_attachment_text_scanned(self):
        set_rules([CLOUD_RULE])
        hit = input_guard.check(["summarize this", "invoice_report INV-2024-0001 totals"], FakePrincipal(), True)
        self.assertIsNotNone(hit)

    def test_validate_rules(self):
        clean, problems = input_guard.validate_rules([
            {"name": "ok", "patterns": ["abc"], "scope": "cloud_only"},
            {"name": "bad-regex", "patterns": ["([bad"], "scope": "block_all"},
            {"name": "bad-scope", "patterns": ["abc"], "scope": "weird"},
            {"name": "no-patterns", "patterns": [], "scope": "block_all"},
            "not-a-dict",
        ])
        self.assertEqual(len(clean), 1)
        self.assertEqual(clean[0]["scope"], "cloud_only")
        self.assertEqual(len(problems), 5)
        self.assertIn("bad-regex", " | ".join(problems))


if __name__ == "__main__":
    unittest.main()
