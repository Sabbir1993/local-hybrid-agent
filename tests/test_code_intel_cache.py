"""R9 cache correctness: index_repo must be stamp-keyed, not blind, and never stale.

Red test: without caching, two index_repo calls return different dicts (is fails);
with caching, edits/adds/deletes must still invalidate, or symbol search silently
answers from a stale snapshot - the worst failure mode for an agent's code search.
"""
import tempfile
import unittest
from pathlib import Path

from core.code_intel import ast_index as idx


class IndexCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ci_cache_")
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _write(self, rel, content):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def test_repeat_query_returns_cached_index(self):
        self._write("a.py", "def foo():\n    return 1\n")
        first = idx.index_repo(self.root)
        second = idx.index_repo(self.root)
        self.assertIs(first, second,
                      "index_repo re-parsed an unchanged repo: every query pays "
                      "full parse cost (fails the <50ms p95 scale acceptance)")

    def test_cache_sees_edited_file(self):
        p = self._write("a.py", "def foo():\n    return 1\n")
        idx.index_repo(self.root)
        p.write_text("def bar():\n    return 2\n", encoding="utf-8")
        data = idx.index_repo(self.root)
        names = {s["name"] for s in data["symbols"]}
        self.assertIn("bar", names)
        self.assertNotIn("foo", names, "stale cache: renamed symbol still indexed")

    def test_cache_sees_added_and_deleted_files(self):
        self._write("a.py", "def foo():\n    return 1\n")
        idx.index_repo(self.root)
        self._write("b.py", "def foo():\n    return 2\n")
        data = idx.index_repo(self.root)
        self.assertEqual(len(idx.find_symbol_definition("foo", self.root)), 2)
        (self.root / "b.py").unlink()
        data = idx.index_repo(self.root)
        hits = [s for s in data["symbols"] if s["name"] == "foo"]
        self.assertEqual(len(hits), 1, "stale cache: deleted file still indexed")
        self.assertEqual(hits[0]["file"], "a.py")

    def test_same_size_rewrite_in_one_clock_tick_is_not_served_stale(self):
        # (mtime_ns, size) cannot see this: measured on Windows, 25 of 40
        # consecutive same-size rewrites shared an mtime because the clock
        # resolution is 15.6ms. The index must still notice - serving a stale
        # symbol list for the file the agent just edited is the worst outcome.
        p = self._write("a.py", "def foo():\n    return 1\n")
        self.assertIn("foo", {s["name"] for s in idx.index_repo(self.root)["symbols"]})
        p.write_text("def bar():\n    return 2\n", encoding="utf-8")
        names = {s["name"] for s in idx.index_repo(self.root)["symbols"]}
        self.assertIn("bar", names, "same-size edit in the same tick was served stale")
        self.assertNotIn("foo", names)

    def test_distinct_roots_do_not_share_entries(self):
        with tempfile.TemporaryDirectory(prefix="ci_cache2_") as other:
            self._write("a.py", "def foo():\n    return 1\n")
            Path(other, "a.py").write_text("def other():\n    pass\n",
                                           encoding="utf-8")
            mine = idx.index_repo(self.root)
            theirs = idx.index_repo(Path(other))
        self.assertIsNot(mine, theirs)
        self.assertIn("foo", {s["name"] for s in mine["symbols"]})
        self.assertIn("other", {s["name"] for s in theirs["symbols"]})


if __name__ == "__main__":
    unittest.main()
