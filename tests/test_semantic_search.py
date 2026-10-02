import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from core.memory import (
    has_fts5,
    has_sqlite_vec,
    reciprocal_rank_fusion,
    search_fts5,
    search_vec_sqlite,
)
from core.memory import store as store_mod


class SemanticSearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        import core.memory as memory_pkg

        self._saved_mem_conn = store_mod._conn
        self._saved_pkg_conn = getattr(memory_pkg, "_conn", None)
        self._had_mem_file = hasattr(memory_pkg, "MEMORY_DB_FILE")
        self._saved_mem_file = getattr(memory_pkg, "MEMORY_DB_FILE", None)

        store_mod._conn = None
        memory_pkg._conn = None
        memory_pkg.MEMORY_DB_FILE = Path(self.tmp.name) / "memory.db"

        # Initialize fresh DB with FTS5 and sqlite-vec
        self.db = store_mod._db()

    def tearDown(self):
        import core.memory as memory_pkg

        try:
            self.db.close()
        except Exception:
            pass
        store_mod._conn = self._saved_mem_conn
        memory_pkg._conn = self._saved_pkg_conn
        if self._had_mem_file:
            memory_pkg.MEMORY_DB_FILE = self._saved_mem_file
        elif hasattr(memory_pkg, "MEMORY_DB_FILE"):
            delattr(memory_pkg, "MEMORY_DB_FILE")
        self.tmp.cleanup()

    def test_extension_probes(self):
        self.assertTrue(has_fts5(), "FTS5 should be supported and active")
        # In python environment with sqlite-vec installed, has_sqlite_vec should be True
        self.assertTrue(has_sqlite_vec(), "sqlite-vec should be loaded")

    def test_fts5_indexing_and_search(self):
        # Insert test chunks directly
        self.db.execute(
            "INSERT INTO chunks(source, path, chunk_idx, text) VALUES ('knowledge', 'kb:1', 0, 'Quantum teleportation astral rates and guidelines')"
        )
        self.db.execute(
            "INSERT INTO chunks(source, path, chunk_idx, text) VALUES ('knowledge', 'kb:2', 0, 'Standard subway travel reimbursement vouchers')"
        )
        self.db.commit()

        # Search via FTS5
        hits = search_fts5("teleportation astral", k=5, sources={"knowledge"})
        self.assertTrue(len(hits) >= 1)
        self.assertEqual(hits[0]["path"], "kb:1")
        self.assertIn("teleportation", hits[0]["text"].lower())

    def test_sqlite_vec_cosine_search(self):
        # Insert vectors (4-dim for test)
        v1 = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32).tobytes()
        v2 = np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32).tobytes()

        self.db.execute(
            "INSERT INTO chunks(source, path, chunk_idx, text, vec, dim) VALUES ('workspace', 'src/a.py', 0, 'vector A', ?, 4)",
            (sqlite3.Binary(v1),),
        )
        self.db.execute(
            "INSERT INTO chunks(source, path, chunk_idx, text, vec, dim) VALUES ('workspace', 'src/b.py', 0, 'vector B', ?, 4)",
            (sqlite3.Binary(v2),),
        )
        self.db.commit()

        # Query vector close to v1
        qvec = [0.95, 0.05, 0.0, 0.0]
        hits = search_vec_sqlite(qvec, k=2, sources={"workspace"})
        self.assertTrue(len(hits) >= 1)
        self.assertEqual(hits[0]["path"], "src/a.py")
        self.assertGreater(hits[0]["cos"], 0.9)

    def test_rrf_fusion(self):
        dense_hits = [
            {"source": "kb", "path": "doc1", "text": "doc1 text"},
            {"source": "kb", "path": "doc2", "text": "doc2 text"},
        ]
        sparse_hits = [
            {"source": "kb", "path": "doc2", "text": "doc2 text"},
            {"source": "kb", "path": "doc3", "text": "doc3 text"},
        ]
        fused = reciprocal_rank_fusion(dense_hits, sparse_hits, k=60)
        self.assertEqual(len(fused), 3)
        # doc2 appears in both, so it should rank #1
        self.assertEqual(fused[0]["path"], "doc2")


if __name__ == "__main__":
    unittest.main()
