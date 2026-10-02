import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from core.memory import (
    expand_query,
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


# ---------------------------------------------------------------------------
# Phase 1 NEW TESTS: Query Expansion + Multi-Variant RRF Paraphrase Recall
# ---------------------------------------------------------------------------

class QueryExpansionTests(unittest.TestCase):
    """Unit tests for the synonym-based expand_query() function."""

    def test_pto_expansion(self):
        """'pto' maps to vacation-leave synonyms."""
        variants = expand_query("do pto days roll over", max_variants=2)
        self.assertTrue(len(variants) >= 1, "Expected at least one variant for 'pto'")
        combined = " ".join(variants)
        # Should include a synonym for either 'pto' or 'roll over'
        self.assertTrue(
            any(w in combined for w in ["vacation", "annual leave", "paid time off",
                                         "carryover", "carry over", "accrue"]),
            f"Expected PTO/rollover synonym in variants, got: {variants}"
        )

    def test_vacation_expansion(self):
        """'vacation' maps to annual leave / paid leave synonyms."""
        variants = expand_query("how many vacation days do I get", max_variants=2)
        self.assertTrue(len(variants) >= 1)
        combined = " ".join(variants)
        self.assertTrue(
            any(w in combined for w in ["annual leave", "paid leave", "holiday", "pto"]),
            f"Expected vacation synonym in variants, got: {variants}"
        )

    def test_refund_expansion(self):
        """'refund' maps to return/reimbursement synonyms."""
        variants = expand_query("can I get a refund on this item", max_variants=2)
        self.assertTrue(len(variants) >= 1)
        combined = " ".join(variants)
        self.assertTrue(
            any(w in combined for w in ["return", "reimbursement", "money back"]),
            f"Expected refund synonym in variants, got: {variants}"
        )

    def test_deploy_expansion(self):
        """'deploy' maps to release/ship/rollout synonyms."""
        variants = expand_query("how do I deploy to production", max_variants=2)
        self.assertTrue(len(variants) >= 1)
        combined = " ".join(variants)
        self.assertTrue(
            any(w in combined for w in ["release", "ship", "push", "rollout",
                                         "prod", "live environment"]),
            f"Expected deploy synonym in variants, got: {variants}"
        )

    def test_no_synonym_returns_empty(self):
        """A query with no known synonym clusters returns empty list, not the original."""
        variants = expand_query("what is the square root of 144", max_variants=2)
        # No synonym for math queries; must return [] not crash
        self.assertIsInstance(variants, list)
        # Original query must NOT be included — expand_query returns variants only
        self.assertNotIn("what is the square root of 144", variants)

    def test_max_variants_respected(self):
        """Never returns more than max_variants items."""
        # A compound query that could match many synonym clusters
        q = "vacation pto rollover refund return shipping"
        for max_v in (0, 1, 2, 3):
            variants = expand_query(q, max_variants=max_v)
            self.assertLessEqual(len(variants), max_v,
                                 f"Got {len(variants)} variants, expected <= {max_v}")

    def test_original_not_included(self):
        """expand_query must never include the original query in its return list."""
        q = "how long do I have to return something"
        variants = expand_query(q, max_variants=3)
        self.assertNotIn(q, variants)
        self.assertNotIn(q.lower(), variants)

    def test_variants_are_different_from_original(self):
        """Every returned variant must differ from the original query."""
        q = "do unused pto days roll over"
        variants = expand_query(q, max_variants=3)
        for v in variants:
            self.assertNotEqual(v.strip(), q.strip(),
                                f"Variant is identical to original: {v!r}")


class MultiVariantRRFRecallTests(unittest.TestCase):
    """Integration tests proving paraphrase recall with multi-variant RRF.

    The core test validates the real-world failure scenario from eval_results.json:
    query "do unused PTO days roll over?" previously missed because the document
    uses "carryover" vocabulary but the query uses "rollover" vocabulary.
    With expand_query() generating a "carryover" variant, the document now
    rises to the top via RRF even if the original query scores zero against it.
    """

    def _make_ranked(self, docs):
        """Build a ranked list in the format expected by reciprocal_rank_fusion."""
        return [{"source": "kb", "path": f"kb:{i}", "text": t}
                for i, t in enumerate(docs)]

    def test_paraphrase_rrf_lifts_synonym_match(self):
        """A doc matching the synonym variant but not the original rises via RRF.

        With k=60 and 3 items, pto-cashout (rank 0+2) and vacation-policy (rank 2+0)
        score identically: 1/60 + 1/62 = 1/62 + 1/60. The key property to test
        is that vacation-policy is *no longer last* — it rises into the top half.
        Before query expansion, vacation-policy was always rank 2 (dead last).
        After RRF with the synonym variant, it ties for 1st or is at 2nd.
        """
        # Original query ranks "vacation-policy" last (no lexical overlap with "rollover")
        original_ranked = [
            {"source": "kb", "path": "pto-cashout", "text": "PTO cashout policy"},
            {"source": "kb", "path": "sick-leave", "text": "sick leave policy"},
            {"source": "kb", "path": "vacation-policy", "text": "vacation carryover accrual"},
        ]
        # Synonym variant "carryover" query ranks "vacation-policy" first
        variant_ranked = [
            {"source": "kb", "path": "vacation-policy", "text": "vacation carryover accrual"},
            {"source": "kb", "path": "pto-cashout", "text": "PTO cashout policy"},
            {"source": "kb", "path": "sick-leave", "text": "sick leave policy"},
        ]
        fused = reciprocal_rank_fusion(original_ranked, variant_ranked, k=60)
        top_paths = [r["path"] for r in fused]
        # vacation-policy must be in top-2 (was dead-last before expansion)
        self.assertIn("vacation-policy", top_paths[:2],
                      f"vacation-policy must rise to top-2 via RRF, got: {top_paths}")

    def test_rrf_preserves_strong_original_match(self):
        """A document that ranks #1 in the original query stays #1 after RRF."""
        original_ranked = [
            {"source": "kb", "path": "refund-policy", "text": "refund policy"},
            {"source": "kb", "path": "shipping", "text": "shipping info"},
        ]
        variant_ranked = [
            {"source": "kb", "path": "refund-policy", "text": "refund policy"},
            {"source": "kb", "path": "shipping", "text": "shipping info"},
        ]
        fused = reciprocal_rank_fusion(original_ranked, variant_ranked, k=60)
        self.assertEqual(fused[0]["path"], "refund-policy")

    def test_expand_query_produces_carryover_for_rollover(self):
        """The specific failing eval case: 'roll over' -> 'carryover' variant."""
        variants = expand_query("do unused pto days roll over", max_variants=2)
        combined = " ".join(variants).lower()
        self.assertTrue(
            "carryover" in combined or "carry over" in combined or "accrue" in combined,
            f"Expected carryover/accrue in variants, got: {variants}"
        )


# ---------------------------------------------------------------------------
# Phase 1 NEW TESTS: SQLite Persistent AST Symbol Cache
# ---------------------------------------------------------------------------

class ASTSQLiteCacheTests(unittest.TestCase):
    """Tests for the persistent SQLite-backed warm-start in ast_index.index_repo()."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        # Write a minimal Python file to the temp repo
        (self.repo / "calc.py").write_text(
            'def add(x, y):\n    """Add two numbers."""\n    return x + y\n\n'
            'def subtract(x, y):\n    return x - y\n',
            encoding="utf-8"
        )

    def tearDown(self):
        from core.code_intel import ast_index as idx
        # Clear in-process cache for this temp path
        idx._CACHE.pop(str(self.repo), None)
        self.tmp.cleanup()

    def test_cold_parse_writes_sqlite_cache(self):
        """After a cold parse, .ast_cache.db should exist with symbol data."""
        from core.code_intel import ast_index as idx
        idx._CACHE.pop(str(self.repo), None)  # force cold
        data = idx.index_repo(self.repo)
        self.assertGreater(data["files"], 0)
        cache_db = self.repo / ".ast_cache.db"
        self.assertTrue(cache_db.exists(), ".ast_cache.db should be written after cold parse")

    def test_warm_start_from_sqlite(self):
        """After clearing the in-process cache, index_repo should load from SQLite."""
        from core.code_intel import ast_index as idx
        # Cold parse → writes SQLite
        idx._CACHE.pop(str(self.repo), None)
        data_cold = idx.index_repo(self.repo)
        # Clear in-process dict to force SQLite path
        idx._CACHE.pop(str(self.repo), None)
        # Warm-start from SQLite
        data_warm = idx.index_repo(self.repo)
        cold_names = sorted(s["name"] for s in data_cold["symbols"])
        warm_names = sorted(s["name"] for s in data_warm["symbols"])
        self.assertEqual(cold_names, warm_names,
                         "SQLite warm-start should return identical symbols as cold parse")

    def test_sqlite_cache_invalidated_on_file_edit(self):
        """Editing a file should bypass the SQLite cache and trigger a fresh parse."""
        from core.code_intel import ast_index as idx
        import time as _time
        idx._CACHE.pop(str(self.repo), None)
        # Cold parse
        idx.index_repo(self.repo)
        idx._CACHE.pop(str(self.repo), None)
        # Modify the file (add a new function)
        _time.sleep(0.02)  # ensure mtime changes
        (self.repo / "calc.py").write_text(
            'def add(x, y):\n    return x + y\n\n'
            'def subtract(x, y):\n    return x - y\n\n'
            'def multiply(x, y):\n    return x * y\n',
            encoding="utf-8"
        )
        data_after = idx.index_repo(self.repo)
        names = [s["name"] for s in data_after["symbols"]]
        self.assertIn("multiply", names,
                      "New symbol 'multiply' should appear after file edit invalidates SQLite cache")


if __name__ == "__main__":
    unittest.main()
