"""tests/test_checkpoint.py - per-run git checkpoints and rewind.

Phase A2: the checkpoint table, the stash-create recording, the ref-namespaced
rewind (never resets a ref this module did not create) and pruning. The git
execution is injected so everything here runs without a companion; the loop
hook and the /git/rewind route are covered by test_git_routes / the live eval.

Run: python -m unittest tests.test_checkpoint -v
"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.agent_loop import checkpoint as cp


class _TempUsageDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "usage.db"
        self._saved_file = cp.USAGE_DB_FILE
        cp.USAGE_DB_FILE = self.db

    def tearDown(self):
        try:
            cp._checkpoint_db().close()
        except Exception:
            pass
        cp.USAGE_DB_FILE = self._saved_file
        self.tmp.cleanup()


class CheckpointTests(_TempUsageDb):
    def test_ref_shape_is_namespaced(self):
        self.assertEqual(cp.checkpoint_ref("run123", 2), "checkpoints/run_run123_step2")

    def test_create_records_and_returns_hash(self):
        calls = []

        async def git(args):
            calls.append(args)
            if args == ["stash", "create"]:
                return 0, "abc123def\n", ""
            return 0, "", ""

        ref = asyncio.run(cp.create_checkpoint("r1", 7, "s1", 0, git))
        self.assertEqual(ref, "abc123def")
        self.assertEqual(calls, [["stash", "create"]])
        rows = cp.list_checkpoints("r1", 7)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ref"], "abc123def")

    def test_create_noop_when_git_empty(self):
        async def git(args):
            return 0, "", ""

        self.assertIsNone(asyncio.run(cp.create_checkpoint("r1", 7, "s1", 0, git)))
        self.assertEqual(cp.list_checkpoints("r1", 7), [])

    def test_rewind_uses_recorded_hash_only(self):
        git_calls = []

        async def git(args):
            git_calls.append(args)
            if args == ["stash", "create"]:
                return 0, "deadbeef\n", ""
            raise AssertionError(f"unexpected git call: {args}")

        asyncio.run(cp.create_checkpoint("r1", 7, "s1", 0, git))
        git_calls.clear()

        async def reset_git(args):
            git_calls.append(args)
            return 0, "", ""

        got = asyncio.run(cp.rewind("r1", 0, 7, reset_git))
        self.assertEqual(got, "deadbeef")
        self.assertEqual(git_calls, [["reset", "--hard", "deadbeef"]])

    def test_rewind_unknown_run_or_step_returns_none(self):
        async def git(args):
            raise AssertionError(f"must not call git: {args}")

        self.assertIsNone(asyncio.run(cp.rewind("nope", 0, 7, git)))
        self.assertIsNone(asyncio.run(cp.rewind("r1", 99, 7, git)))

    def test_prune_keeps_newest_only(self):
        async def git(args):
            return 0, "c", ""

        for i in range(7):
            asyncio.run(cp.create_checkpoint("r1", 7, "s1", i, git))
        rows = cp.list_checkpoints("r1", 7)
        self.assertEqual(len(rows), cp.CHECKPOINT_MAX_DEFAULT)  # 5 newest steps
        self.assertEqual([r["step"] for r in rows], [2, 3, 4, 5, 6])


if __name__ == "__main__":
    unittest.main()
