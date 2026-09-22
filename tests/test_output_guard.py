"""tests/test_output_guard.py - Unit tests for core/output_guard.py.

Run: python -m unittest tests.test_output_guard -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import output_guard, input_guard


class FakePrincipal:
    def __init__(self, username="alice", roles=("user",)):
        self.id = 1
        self.username = username
        self.role_names = list(roles)


def set_rules(rules, enabled=True):
    from core import small_model
    small_model.APP_CONFIG["output_guard"] = {"enabled": enabled, "rules": rules}
    input_guard._compile_cache.clear()
    input_guard._cache_ts = 0.0


CLOUD_RULE = {
    "id": "r1", "name": "No card numbers", "patterns": [r"\b\d{13,19}\b"],
    "scope": "cloud_only", "roles": [], "users": [],
    "message": "Card numbers are redacted from cloud responses.",
}


class TestOutputGuard(unittest.TestCase):
    def tearDown(self):
        set_rules([])

    def test_redact_full_single_text(self):
        set_rules([CLOUD_RULE])
        out, hit = output_guard.redact_full("Your card is 4111111111111111 ok", FakePrincipal(), True)
        self.assertNotIn("4111111111111111", out)
        self.assertIn("█████", out)
        self.assertEqual(hit["name"], "No card numbers")

    def test_cloud_only_not_applied_to_local(self):
        set_rules([CLOUD_RULE])
        out, hit = output_guard.redact_full("card 4111111111111111", FakePrincipal(), False)
        self.assertIn("4111111111111111", out)
        self.assertIsNone(hit)

    def test_streaming_match_within_delta(self):
        set_rules([CLOUD_RULE])
        red = output_guard.OutputRedactor(FakePrincipal(), True)
        # nothing is emitted while inside the holdback window
        self.assertEqual(red.feed("card "), "")
        self.assertEqual(red.feed("4111111111111111"), "")
        # end of stream releases everything, redacted
        tail = red.flush()
        self.assertNotIn("4111111111111111", tail)
        self.assertIn("█████", tail)
        self.assertEqual(red.matched["name"], "No card numbers")

    def test_streaming_match_across_deltas(self):
        set_rules([CLOUD_RULE])
        red = output_guard.OutputRedactor(FakePrincipal(), True)
        chunks = [red.feed(c) for c in ("abc 411111", "11111111", "11 xyz")]
        chunks.append(red.flush())
        whole = "".join(chunks)
        self.assertNotIn("4111111111111111", whole)
        self.assertIn("█████", whole)
        self.assertIn("abc", whole)
        self.assertIn("xyz", whole)

    def test_no_rules_passthrough_zero_hold(self):
        set_rules([])
        red = output_guard.OutputRedactor(FakePrincipal(), True)
        self.assertEqual(red.hold, 0)
        self.assertEqual(red.feed("hello "), "hello ")
        self.assertEqual(red.feed("world"), "world")
        self.assertEqual(red.flush(), "")

    def test_flush_releases_tail(self):
        set_rules([CLOUD_RULE])
        red = output_guard.OutputRedactor(FakePrincipal(), True)
        red.feed("short")
        self.assertEqual(red.flush(), "short")

    def test_reset_drops_pending_holdback(self):
        set_rules([CLOUD_RULE])
        red = output_guard.OutputRedactor(FakePrincipal(), True)
        red.feed("pending tail text")
        red.reset()
        self.assertEqual(red.flush(), "")

    def test_custom_replacement(self):
        set_rules([dict(CLOUD_RULE, replacement="[REDACTED]")])
        out, _ = output_guard.redact_full("card 4111111111111111", FakePrincipal(), True)
        self.assertIn("[REDACTED]", out)

    def test_role_targeting(self):
        set_rules([dict(CLOUD_RULE, roles=["intern"], scope="block_all")])
        out, hit = output_guard.redact_full("card 4111111111111111",
                                            FakePrincipal(roles=("intern",)), False)
        self.assertIsNotNone(hit)
        out2, hit2 = output_guard.redact_full("card 4111111111111111",
                                              FakePrincipal(roles=("user",)), False)
        self.assertIsNone(hit2)
        self.assertIn("4111111111111111", out2)

    def test_guard_event_once_semantics(self):
        set_rules([CLOUD_RULE])
        red = output_guard.OutputRedactor(FakePrincipal(), True)
        red.feed("card 4111111111111111")
        red.flush()
        self.assertIsNotNone(red.matched)
        self.assertGreaterEqual(red.hits, 1)

    def test_disabled_guard_passthrough(self):
        set_rules([CLOUD_RULE], enabled=False)
        out, hit = output_guard.redact_full("card 4111111111111111", FakePrincipal(), True)
        self.assertIsNone(hit)
        self.assertIn("4111111111111111", out)


if __name__ == "__main__":
    unittest.main()
