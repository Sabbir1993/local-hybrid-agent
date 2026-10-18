"""tests/test_kb_atomic_reindex.py - a reindex keeps the old chunks searchable until the new set is stored,
and a failed rebuild leaves the old set in place.

Run: python -m unittest tests.test_kb_atomic_reindex -v
"""

import asyncio
import sqlite3
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.memory as mem
from core.memory import indexing


class _TL:
    """Stand-in for the thread-local DB: one shared in-memory connection."""
    def __init__(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.execute("CREATE TABLE chunks (source TEXT, path TEXT, chunk_idx INT, text TEXT, vec BLOB, "
                          "dim INT, version REAL, UNIQUE(source, path, chunk_idx))")
        self.conn.commit()


def _store(db):
    def store(rows, vecs):
        db.conn.executemany("INSERT OR REPLACE INTO chunks (source, path, chunk_idx, text, version) VALUES (?,?,?,?,?)", rows)
        db.conn.commit()
    return store


class AtomicReindex(unittest.TestCase):
    def setUp(self):
        self.db = _TL()
        self.db.conn.executemany("INSERT INTO chunks (source, path, chunk_idx, text, version) VALUES ('knowledge','kb:5',?,?,1)",
                                 [(0, "old zero"), (1, "old one"), (2, "old two")])
        self.db.conn.commit()
        p = mock.patch.object(indexing, "_db", lambda: self.db.conn)
        p.start()
        self.addCleanup(p.stop)

    def _texts(self):
        return [r[0] for r in self.db.conn.execute("SELECT text FROM chunks WHERE path='kb:5' ORDER BY chunk_idx")]

    def test_old_chunks_visible_while_embedding(self):
        seen = {}

        async def slow_embed(texts):
            seen["during"] = self._texts()       # what a search would see mid-reindex
            return [[0.1, 0.2]] * len(texts)

        with mock.patch.object(mem, "_embed_texts", slow_embed), mock.patch.object(mem, "_store_vecs", _store(self.db)):
            n = asyncio.run(indexing.index_knowledge_source(5, "fresh content here"))
        self.assertEqual(seen["during"], ["old zero", "old one", "old two"])
        self.assertEqual(n, 1)
        self.assertEqual(len(self._texts()), 1)               # stale extra chunks are gone after the swap

    def test_failed_embed_keeps_old_chunks(self):
        async def boom(texts):
            raise RuntimeError("embedder crashed")

        with mock.patch.object(mem, "_embed_texts", boom), mock.patch.object(mem, "_store_vecs", _store(self.db)):
            with self.assertRaises(RuntimeError):
                asyncio.run(indexing.index_knowledge_source(5, "fresh content here"))
        self.assertEqual(self._texts(), ["old zero", "old one", "old two"])

    def test_failed_store_rolls_back_the_delete(self):
        async def emb(texts):
            return [[0.1]] * len(texts)

        def bad_store(rows, vecs):
            raise sqlite3.OperationalError("disk full")

        with mock.patch.object(mem, "_embed_texts", emb), mock.patch.object(mem, "_store_vecs", bad_store):
            with self.assertRaises(sqlite3.OperationalError):
                asyncio.run(indexing.index_knowledge_source(5, "fresh content here"))
        self.assertEqual(self._texts(), ["old zero", "old one", "old two"])

    def test_empty_new_text_clears_the_source(self):
        async def emb(texts):
            return None

        with mock.patch.object(mem, "_embed_texts", emb), mock.patch.object(mem, "_store_vecs", _store(self.db)):
            asyncio.run(indexing.index_knowledge_source(5, ""))
        self.assertEqual(self._texts(), [])


if __name__ == "__main__":
    unittest.main()
