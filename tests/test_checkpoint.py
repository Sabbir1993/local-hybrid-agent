"""tests/test_checkpoint.py - per-run git checkpoints and rewind.

Phase A2: the checkpoint table, the stash-create recording, the ref-namespaced
rewind (never resets a ref this module did not create) and pruning. The git
execution is injected so everything here runs without a companion; the loop
hook and the /git/rewind route are covered by test_git_routes / the live eval.

Run: python -m unittest tests.test_checkpoint -v
"""

import asyncio
import shutil
import subprocess
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

    def test_create_records_the_stash_commit_and_untracked_files(self):
        calls = []

        async def git(args):
            calls.append(args)
            if args == ["stash", "create"]:
                return 0, "abc123def\n", ""
            if args[:1] == ["ls-files"]:
                return 0, "notes.txt\nsrc/new.py\n", ""
            return 0, "", ""

        ref = asyncio.run(cp.create_checkpoint("r1", 7, "s1", 0, git))
        self.assertEqual(ref, "abc123def")
        self.assertEqual(calls[0], ["stash", "create"])
        rows = cp.list_checkpoints("r1", 7)
        self.assertEqual((len(rows), rows[0]["ref"]), (1, "abc123def"))

    def test_clean_tree_checkpoints_at_head(self):
        """`git stash create` prints nothing when tracked files are clean: that used to mean no checkpoint."""
        async def git(args):
            if args == ["stash", "create"]:
                return 0, "", ""
            if args == ["rev-parse", "HEAD"]:
                return 0, "headhash\n", ""
            return 0, "", ""

        self.assertEqual(asyncio.run(cp.create_checkpoint("r1", 7, "s1", 0, git)), "headhash")

    def test_no_checkpoint_when_there_is_nothing_to_anchor_to(self):
        async def git(args):
            return (0, "", "") if args == ["stash", "create"] else (128, "", "no commits")

        self.assertIsNone(asyncio.run(cp.create_checkpoint("r1", 7, "s1", 0, git)))
        self.assertEqual(cp.list_checkpoints("r1", 7), [])

    def test_rewind_never_uses_a_blocked_git_argument(self):
        seen = []

        async def git(args):
            seen.append(list(args))
            if args == ["stash", "create"]:
                return 0, "deadbeef\n", ""
            return 0, "", ""

        asyncio.run(cp.create_checkpoint("r1", 7, "s1", 0, git))
        seen.clear()
        res = asyncio.run(cp.rewind("r1", 0, 7, git))
        self.assertEqual(res["commit"], "deadbeef")
        flat = [a for call in seen for a in call]
        for banned in ("reset", "--hard", "clean", "update-ref", "-f", "--force", "--delete"):
            self.assertNotIn(banned, flat)
        self.assertIn(["restore", "--source=deadbeef", "--staged", "--worktree", "--", "."], seen)

    def test_rewind_unknown_run_or_step_returns_none(self):
        async def git(args):
            raise AssertionError(f"must not call git: {args}")

        self.assertIsNone(asyncio.run(cp.rewind("nope", 0, 7, git)))
        self.assertIsNone(asyncio.run(cp.rewind("r1", 99, 7, git)))

    def test_another_users_checkpoint_is_not_rewindable(self):
        async def git(args):
            return 0, "abc\n", ""

        asyncio.run(cp.create_checkpoint("r1", 7, "s1", 0, git))
        self.assertIsNone(asyncio.run(cp.rewind("r1", 0, 8, git)))

    def test_a_checkpoint_git_already_pruned_is_reported_not_attempted(self):
        async def make(args):
            return 0, "abc\n", ""

        asyncio.run(cp.create_checkpoint("r1", 7, "s1", 0, make))
        calls = []

        async def gone(args):
            calls.append(args)
            return (1, "", "") if args[0] == "rev-parse" else (0, "", "")

        res = asyncio.run(cp.rewind("r1", 0, 7, gone))
        self.assertIn("no longer exists", res["error"])
        self.assertFalse(any(c[0] == "restore" for c in calls))

    def test_prune_keeps_newest_only(self):
        async def git(args):
            return 0, "c", ""

        for i in range(7):
            asyncio.run(cp.create_checkpoint("r1", 7, "s1", i, git))
        rows = cp.list_checkpoints("r1", 7)
        self.assertEqual(len(rows), cp.CHECKPOINT_MAX_DEFAULT)  # 5 newest steps
        self.assertEqual([r["step"] for r in rows], [2, 3, 4, 5, 6])


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class RealGitRoundTrip(_TempUsageDb):
    """The safety of rewind rests on how `git restore --source` behaves, so it runs against real git."""

    def setUp(self):
        super().setUp()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.g("init", "-q")
        (self.repo / "a.py").write_text("print('one')\n", encoding="utf-8")
        (self.repo / "keep.txt").write_text("keep\n", encoding="utf-8")
        self.g("add", "-A")
        self.g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base")

    def g(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True, check=True).stdout.strip()

    async def git(self, args):
        r = subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True)
        return r.returncode, r.stdout, r.stderr

    def read(self, name):
        return (self.repo / name).read_text(encoding="utf-8")

    def test_rewind_restores_tracked_files_keeps_untracked_and_saves_a_backup(self):
        (self.repo / "a.py").write_text("print('two')\n", encoding="utf-8")            # dirty when the run starts
        (self.repo / "mine.txt").write_text("a file of the user\n", encoding="utf-8")   # untracked before the run
        cp_hash = asyncio.run(cp.create_checkpoint("r1", 7, "s1", 0, self.git))
        self.assertTrue(cp_hash)

        # the agent works: edits a tracked file, adds a new one, deletes another, creates an untracked file
        (self.repo / "a.py").write_text("print('agent')\n", encoding="utf-8")
        (self.repo / "b.py").write_text("new tracked\n", encoding="utf-8")
        self.g("add", "b.py")
        (self.repo / "keep.txt").unlink()
        (self.repo / "scratch.log").write_text("agent output\n", encoding="utf-8")

        res = asyncio.run(cp.rewind("r1", 0, 7, self.git))
        self.assertEqual(res["commit"], cp_hash)
        self.assertEqual(self.read("a.py"), "print('two')\n",
                         "back to the state at the checkpoint, including the user's pre-run edit")
        self.assertEqual(self.read("keep.txt"), "keep\n", "the deleted file is back")
        self.assertFalse((self.repo / "b.py").exists(), "a tracked file the agent added is gone")
        self.assertEqual(self.read("mine.txt"), "a file of the user\n")
        self.assertTrue((self.repo / "scratch.log").exists(), "untracked files are never deleted")
        self.assertEqual(res["left_untracked"], ["scratch.log"], "...but the user is told about them")

        # the state just before the rewind was saved and can be brought back with the mirror command
        # (`git stash apply` would conflict with the now-restored files)
        self.assertTrue(res["backup"])
        self.g("restore", "--source=" + res["backup"], "--staged", "--worktree", "--", ".")
        self.assertEqual(self.read("a.py"), "print('agent')\n")

    def test_clean_repo_rewinds_to_head(self):
        asyncio.run(cp.create_checkpoint("r2", 7, "s1", 0, self.git))
        (self.repo / "a.py").write_text("print('changed')\n", encoding="utf-8")
        res = asyncio.run(cp.rewind("r2", 0, 7, self.git))
        self.assertEqual(self.read("a.py"), "print('one')\n")
        self.assertEqual(res["commit"], self.g("rev-parse", "HEAD"))

    def test_rewind_with_nothing_changed_is_a_noop_without_a_backup(self):
        asyncio.run(cp.create_checkpoint("r3", 7, "s1", 0, self.git))
        res = asyncio.run(cp.rewind("r3", 0, 7, self.git))
        self.assertIsNone(res["backup"])
        self.assertEqual(self.read("a.py"), "print('one')\n")

    def test_the_destructive_guards_still_refuse_what_rewind_no_longer_needs(self):
        from core import git_tools
        code, _o, err = asyncio.run(git_tools._run(["reset", "--hard", "HEAD"], self.repo))
        self.assertEqual(code, 1)
        self.assertIn("refused", err)


if __name__ == "__main__":
    unittest.main()
