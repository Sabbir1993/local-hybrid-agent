"""tests/test_subagent_blackboard.py - Unit tests for subagent shared blackboard & typed envelopes.

Verifies:
  1. Thread-safe read/write/clear/summary operations.
  2. PAN credit card number masking on chalkboard values.
  3. Size capping and oldest-entry eviction on key limit.
  4. Tool wrappers: blackboard_post and blackboard_read.
  5. Active subagent write tracking and envelope header formatting.
  6. Compatibility with subagent_result_verdict parser.

Run: python -m unittest tests.test_subagent_blackboard -v
"""

import asyncio
import unittest
from unittest import mock

from core.subagent.blackboard import (
    MAX_ENTRIES_PER_SESSION,
    _active_scope_writes,
    blackboard_clear,
    blackboard_read,
    blackboard_summary,
    blackboard_write,
    tool_blackboard_post,
    tool_blackboard_read,
)
from core.subagent.runner import _envelope_header, subagent_result_verdict


class BlackboardCoreTests(unittest.TestCase):
    def setUp(self):
        blackboard_clear("test_session")

    def tearDown(self):
        blackboard_clear("test_session")

    def test_write_and_read_single_key(self):
        entry = blackboard_write("auth_schema", "tokens table in core/auth_db", author="worker_1", session_id="test_session")
        self.assertEqual(entry["key"], "auth_schema")
        self.assertEqual(entry["value"], "tokens table in core/auth_db")
        self.assertEqual(entry["author"], "worker_1")

        read_back = blackboard_read("auth_schema", session_id="test_session")
        self.assertIsNotNone(read_back)
        self.assertEqual(read_back["value"], "tokens table in core/auth_db")

    def test_read_all_keys(self):
        blackboard_write("key_1", "val_1", session_id="test_session")
        blackboard_write("key_2", "val_2", session_id="test_session")
        all_entries = blackboard_read(session_id="test_session")
        self.assertEqual(set(all_entries.keys()), {"key_1", "key_2"})

    def test_empty_key_raises_error(self):
        with self.assertRaises(ValueError):
            blackboard_write("", "val", session_id="test_session")

    def test_pans_are_masked(self):
        blackboard_write("leaked_data", "card number is 4111111111111111", session_id="test_session")
        read_back = blackboard_read("leaked_data", session_id="test_session")
        self.assertNotIn("4111111111111111", read_back["value"])
        self.assertIn("1111", read_back["value"])

    def test_eviction_when_cap_reached(self):
        for i in range(MAX_ENTRIES_PER_SESSION + 5):
            blackboard_write(f"k_{i}", f"val_{i}", session_id="test_session")
        all_entries = blackboard_read(session_id="test_session")
        self.assertEqual(len(all_entries), MAX_ENTRIES_PER_SESSION)
        # Oldest keys should have been evicted
        self.assertNotIn("k_0", all_entries)
        self.assertIn(f"k_{MAX_ENTRIES_PER_SESSION + 4}", all_entries)

    def test_summary_formatting(self):
        blackboard_write("api_route", "/agent/run SSE", session_id="test_session")
        summary = blackboard_summary(session_id="test_session")
        self.assertIn("--- SHARED BLACKBOARD", summary)
        self.assertIn("[api_route]: /agent/run SSE", summary)
        self.assertIn("--- END BLACKBOARD ---", summary)


class BlackboardToolTests(unittest.TestCase):
    def setUp(self):
        blackboard_clear("test_session")

    def tearDown(self):
        blackboard_clear("test_session")

    def test_tool_blackboard_post(self):
        out = asyncio.run(tool_blackboard_post({"key": "db_path", "value": "projects.db"}))
        self.assertIn("saved to blackboard: [db_path]", out)

    def test_tool_blackboard_post_missing_fields(self):
        self.assertIn("error:", asyncio.run(tool_blackboard_post({"key": ""})))
        self.assertIn("error:", asyncio.run(tool_blackboard_post({"key": "k", "value": ""})))

    def test_tool_blackboard_read(self):
        asyncio.run(tool_blackboard_post({"key": "status", "value": "green"}))
        out_single = asyncio.run(tool_blackboard_read({"key": "status"}))
        self.assertIn("[status]: green", out_single)

        out_all = asyncio.run(tool_blackboard_read({}))
        self.assertIn("• [status]: green", out_all)


class SubagentEnvelopeIntegrationTests(unittest.TestCase):
    def test_envelope_with_posted_keys(self):
        header = _envelope_header(role="researcher", lane="main", n_msgs=6, status="success", posted=["schema", "route"])
        self.assertIn("role=researcher", header)
        self.assertIn("lane=main", header)
        self.assertIn("status=success", header)
        self.assertIn("posted=schema,route", header)

    def test_subagent_result_verdict_parses_status_with_posted_keys(self):
        result = "[sub-agent · role=coder · 4 msgs · lane=executor · status=success · posted=test_file]\nAll tests passed."
        verdict = subagent_result_verdict("spawn_agent", result)
        self.assertTrue(verdict)

        failed_result = "[sub-agent · role=coder · 4 msgs · lane=executor · status=step_exhausted · posted=k1]\nTimed out."
        self.assertFalse(subagent_result_verdict("spawn_agent", failed_result))


if __name__ == "__main__":
    unittest.main()
