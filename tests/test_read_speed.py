"""The file-reading speed work: the extraction cache, windowed reads, and the single-parse PDF read."""
import tempfile
import unittest
from pathlib import Path

from core.agent_tools import edit_engine
from core.file_tools import extract_cache


class ExtractCache(unittest.TestCase):
    def setUp(self):
        extract_cache.clear()

    def test_extracts_once_per_file_version(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.txt"
            p.write_text("one", encoding="utf-8")
            calls = []

            def extract(f):
                calls.append(f)
                return f.read_text(encoding="utf-8")
            self.assertEqual(extract_cache.cached_text(p, extract), "one")
            self.assertEqual(extract_cache.cached_text(p, extract), "one")
            self.assertEqual(len(calls), 1)
            p.write_text("changed text", encoding="utf-8")          # size and mtime change: read again
            self.assertEqual(extract_cache.cached_text(p, extract), "changed text")
            self.assertEqual(len(calls), 2)

    def test_forget_drops_the_file(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.txt"
            p.write_text("x", encoding="utf-8")
            calls = []
            extract_cache.cached_text(p, lambda f: calls.append(1) or "x")
            extract_cache.forget(p)
            extract_cache.cached_text(p, lambda f: calls.append(1) or "x")
            self.assertEqual(len(calls), 2)

    def test_user_is_part_of_the_key(self):
        from core import request_context
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.txt"
            p.write_text("x", encoding="utf-8")
            calls = []
            for uid in (1, 2, 1):
                request_context.set_current_user(uid)
                extract_cache.cached_text(p, lambda f: calls.append(1) or "x")
            self.assertEqual(len(calls), 2)                         # user 2 never reads user 1's entry
            request_context.set_current_user(None)


class WindowedRead(unittest.TestCase):
    TEXT = "\n".join(f"line {i}" for i in range(1, 41)) + "\n"

    def test_window_matches_a_slice_of_the_whole_file_view(self):
        whole = edit_engine.numbered(self.TEXT, 11, 10, 20000)
        lines = self.TEXT.split("\n")[:-1]
        win = edit_engine.numbered_window(lines[10:20], 11, len(lines), 20000)
        self.assertEqual(win, whole)

    def test_window_past_the_end_and_at_the_end(self):
        lines = self.TEXT.split("\n")[:-1]
        end = edit_engine.numbered_window(lines[35:], 36, 40, 20000)
        self.assertEqual((end["first"], end["last"], end["next_offset"]), (36, 40, None))
        gone = edit_engine.numbered_window([], 99, 40, 20000)
        self.assertIsNone(gone["first"])


if __name__ == "__main__":
    unittest.main()
