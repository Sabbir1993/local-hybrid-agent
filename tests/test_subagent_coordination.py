"""tests/test_subagent_coordination.py - Unit tests for subagent file locking and structured exit contracts."""

import asyncio
import time
import unittest
from unittest import mock

from core.subagent.blackboard import (
    acquire_file_lock,
    blackboard_clear,
    get_file_lock_holder,
    list_file_locks,
    release_all_file_locks_for_holder,
    release_file_lock,
)
from core.subagent.runner import (
    _envelope_header,
    parse_subagent_envelope,
    subagent_result_verdict,
)


class TestSubagentCoordination(unittest.TestCase):
    def setUp(self):
        blackboard_clear("test_session")

    def tearDown(self):
        blackboard_clear("test_session")

    def test_file_lock_lifecycle(self):
        sid = "test_session"
        # 1. Acquire free file
        ok, holder = acquire_file_lock("src/index.js", "agent_1", timeout_s=10.0, session_id=sid)
        self.assertTrue(ok)
        self.assertEqual(holder, "agent_1")
        self.assertEqual(get_file_lock_holder("src/index.js", session_id=sid), "agent_1")

        # 2. Path normalization: forward vs backslash and case-insensitive drive letter
        self.assertEqual(get_file_lock_holder(r"src\index.js", session_id=sid), "agent_1")

        # 3. Second agent fails to acquire locked file
        ok2, holder2 = acquire_file_lock("src/index.js", "agent_2", timeout_s=10.0, session_id=sid)
        self.assertFalse(ok2)
        self.assertEqual(holder2, "agent_1")

        # 4. Same agent can refresh lock
        ok3, holder3 = acquire_file_lock("src/index.js", "agent_1", timeout_s=20.0, session_id=sid)
        self.assertTrue(ok3)
        self.assertEqual(holder3, "agent_1")

        # 5. List active locks
        locks = list_file_locks(session_id=sid)
        self.assertIn("src/index.js", locks)
        self.assertEqual(locks["src/index.js"]["holder"], "agent_1")

        # 6. Unauthorized release fails
        rel_fail = release_file_lock("src/index.js", "agent_2", session_id=sid)
        self.assertFalse(rel_fail)
        self.assertEqual(get_file_lock_holder("src/index.js", session_id=sid), "agent_1")

        # 7. Authorized release succeeds
        rel_ok = release_file_lock("src/index.js", "agent_1", session_id=sid)
        self.assertTrue(rel_ok)
        self.assertIsNone(get_file_lock_holder("src/index.js", session_id=sid))

        # 8. Agent 2 can now acquire
        ok4, holder4 = acquire_file_lock("src/index.js", "agent_2", timeout_s=10.0, session_id=sid)
        self.assertTrue(ok4)
        self.assertEqual(holder4, "agent_2")

    def test_file_lock_expiration(self):
        sid = "test_session"
        # Acquire with 0.1s timeout
        ok, _ = acquire_file_lock("temp.txt", "agent_old", timeout_s=0.05, session_id=sid)
        self.assertTrue(ok)
        time.sleep(0.08)

        # New agent can reclaim expired lock
        ok2, holder2 = acquire_file_lock("temp.txt", "agent_new", timeout_s=5.0, session_id=sid)
        self.assertTrue(ok2)
        self.assertEqual(holder2, "agent_new")

    def test_release_all_locks_for_holder(self):
        sid = "test_session"
        acquire_file_lock("file1.py", "sub_worker", timeout_s=10.0, session_id=sid)
        acquire_file_lock("file2.py", "sub_worker", timeout_s=10.0, session_id=sid)
        acquire_file_lock("file3.py", "other_worker", timeout_s=10.0, session_id=sid)

        freed = release_all_file_locks_for_holder("sub_worker", session_id=sid)
        self.assertEqual(len(freed), 2)
        self.assertIn("file1.py", freed)
        self.assertIn("file2.py", freed)
        self.assertIsNone(get_file_lock_holder("file1.py", session_id=sid))
        self.assertIsNone(get_file_lock_holder("file2.py", session_id=sid))
        self.assertEqual(get_file_lock_holder("file3.py", session_id=sid), "other_worker")

    def test_structured_envelope_generation_and_parsing(self):
        header = _envelope_header(
            role="coder",
            lane="executor",
            n_msgs=6,
            status="success",
            posted=["schema_verified", "test_passed"],
            files=["core/app.py", "tests/test_app.py"],
        )
        full_text = f"{header}\nCompleted implementing authentication endpoint."

        # Header contains all structured segments
        self.assertIn("role=coder", header)
        self.assertIn("lane=executor", header)
        self.assertIn("status=success", header)
        self.assertIn("posted=schema_verified,test_passed", header)
        self.assertIn("files=core/app.py,tests/test_app.py", header)

        # Standard subagent_result_verdict still reports success
        self.assertTrue(subagent_result_verdict("spawn_agent", full_text))

        # parse_subagent_envelope parses typed dictionary
        parsed = parse_subagent_envelope(full_text)
        self.assertEqual(parsed["status"], "success")
        self.assertEqual(parsed["role"], "coder")
        self.assertEqual(parsed["lane"], "executor")
        self.assertEqual(parsed["posted"], ["schema_verified", "test_passed"])
        self.assertEqual(parsed["files_modified"], ["core/app.py", "tests/test_app.py"])
        self.assertEqual(parsed["content"], "Completed implementing authentication endpoint.")

    def test_subagent_lock_conflict_prevention(self):
        sid = "test_session"
        # Sibling agent acquired lock on target.py
        acquire_file_lock("target.py", "sibling_agent_99", timeout_s=30.0, session_id=sid)

        # Another subagent tries to write to target.py
        ok, holder = acquire_file_lock("target.py", "sub_current", session_id=sid)
        self.assertFalse(ok)
        self.assertEqual(holder, "sibling_agent_99")


if __name__ == "__main__":
    unittest.main()
