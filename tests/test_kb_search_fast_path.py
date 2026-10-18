"""tests/test_kb_search_fast_path.py - the numpy access filter and cached term counts must return exactly
what the plain per-row loops returned.

Run: python -m unittest tests.test_kb_search_fast_path -v
"""

import asyncio
import random
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

import core.memory as mem
from core.memory import cache as mcache, search as msearch

WORDS = "refund policy leave salary merchant settlement bank account branch loan report audit".split()


def _corpus(n=400, dim=16, sources=12):
    rnd = random.Random(5)
    rng = np.random.default_rng(5)
    entries = []
    for i in range(n):
        text = " ".join(rnd.choice(WORDS) for _ in range(rnd.randint(8, 30)))
        src = "knowledge" if i % 9 else "session"
        path = f"kb:{i % sources}" if src == "knowledge" else f"session:{i}"
        vec = rng.standard_normal(dim).astype(np.float32) if i % 17 else None
        entries.append((src, path, text, vec))
    return entries, rng.standard_normal(dim).astype(np.float32)


class FastPathEquivalence(unittest.TestCase):
    def _search(self, built, qvec, allowed, query, **kw):
        async def fake_embed(texts):
            return [qvec.tolist()]

        titles = [{"id": i, "title": f"Doc {i} refund", "status": "ready"} for i in range(12)]
        with mock.patch.object(mem, "_cached_entries", lambda: built), \
             mock.patch.object(mem, "_embed_texts", fake_embed), \
             mock.patch.object(msearch, "search_fts5", lambda *a, **k: []), \
             mock.patch("core.auth_db.list_knowledge_sources", lambda: titles):
            return asyncio.run(msearch.search_knowledge_hybrid(
                query, k=6, allowed_knowledge_source_ids=allowed, min_cos=-1.0, rerank_min_cos=-1.0, **kw))

    def test_same_results_as_plain_loops(self):
        entries, q = _corpus()
        fast = mcache._build_cache(entries)
        slow = mcache._build_cache(entries)
        slow["kids"] = None                                  # forces the original per-row loops
        self.assertIsNotNone(fast["kids"])
        for allowed in ({0, 3, 7}, set(range(12)), {5}, {99}):
            for query in ("refund policy leave", "merchant settlement bank", "audit audit report"):
                for kw in ({}, {"rerank": False}, {"use_title_lex": True}):
                    with self.subTest(allowed=sorted(allowed), q=query, kw=kw):
                        a = self._search(fast, q, allowed, query, **kw)
                        b = self._search(slow, q, allowed, query, **kw)
                        self.assertEqual(a, b)

    def test_term_counts_match_str_count_and_are_cached(self):
        entries, _ = _corpus(50)
        c = mcache._build_cache(entries)
        arr = mcache._term_counts(c, "refund")
        self.assertEqual(arr.tolist(), [t.count("refund") for t in c["lower"]])
        self.assertIs(mcache._term_counts(c, "refund"), arr)

    def test_term_cache_is_bounded(self):
        entries, _ = _corpus(20)
        c = mcache._build_cache(entries)
        for i in range(mcache._TERM_CACHE_MAX + 10):
            mcache._term_counts(c, f"w{i}")
        self.assertLessEqual(len(c["wc"]), mcache._TERM_CACHE_MAX)

    def test_kids_array(self):
        entries = [("knowledge", "kb:4", "a", None), ("session", "session:1", "b", None),
                   ("knowledge", "kb:x", "c", None)]
        c = mcache._build_cache(entries)
        self.assertEqual(c["kids"].tolist(), [4, -1, -1])


if __name__ == "__main__":
    unittest.main()
