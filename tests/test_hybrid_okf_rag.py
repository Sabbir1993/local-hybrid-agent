import unittest
import asyncio
from unittest.mock import patch, MagicMock
from core.memory.okf import (
    init_okf_tables,
    extract_okf_graph,
    index_okf_source,
    delete_okf_source,
    search_okf,
)
from core.memory.reranker import (
    deduplicate_candidates,
    calculate_cross_features,
    rerank_and_filter,
)
from core.knowledge_router import fetch_company_knowledge
from core.memory.store import _db


class TestHybridOkfRag(unittest.TestCase):
    def setUp(self):
        self.db = _db()
        init_okf_tables(self.db)
        delete_okf_source(9999)

    def tearDown(self):
        delete_okf_source(9999)

    def test_okf_extraction_and_storage(self):
        sample_doc = (
            "# Engineering Guidelines\n"
            "## Architecture Standards\n"
            "Alice is the lead architect for Payment Gateway.\n"
            "Bob reports to Alice.\n"
            "Payment Gateway depends on Redis Cache.\n"
            "SLA: 99.99% uptime required.\n"
        )
        chunks = [
            "Alice is the lead architect for Payment Gateway. Bob reports to Alice.",
            "Payment Gateway depends on Redis Cache. SLA: 99.99% uptime required."
        ]
        n_ent, n_rel, n_links = index_okf_source(9999, sample_doc, chunks)
        self.assertGreater(n_ent, 0)
        self.assertGreater(n_rel, 0)
        self.assertGreaterEqual(n_links, 1)

        # Search OKF
        facts = search_okf("Who reports to Alice?", allowed_source_ids={9999})
        self.assertTrue(len(facts) > 0)
        bob_fact = any("Bob" in f["fact"] and "Alice" in f["fact"] for f in facts)
        self.assertTrue(bob_fact, f"Expected Bob -> Alice relation in {facts}")

        # Search dependency
        dep_facts = search_okf("What does Payment Gateway depend on?", allowed_source_ids={9999})
        self.assertTrue(len(dep_facts) > 0)
        self.assertTrue(any("Redis" in f["fact"] for f in dep_facts))

        # Cleanup test
        delete_okf_source(9999)
        cleared_facts = search_okf("Who reports to Alice?", allowed_source_ids={9999})
        self.assertEqual(len(cleared_facts), 0)

    def test_reranker_deduplication(self):
        candidates = [
            {"source_id": 1, "chunk_idx": 0, "text": "Annual leave policy: 20 days per year."},
            {"source_id": 1, "chunk_idx": 0, "text": "Annual leave policy: 20 days per year."},  # duplicate
            {"source_id": 1, "chunk_idx": 1, "text": "Sick leave policy: 10 days per year."},
        ]
        deduped = deduplicate_candidates(candidates)
        self.assertEqual(len(deduped), 2)
        self.assertEqual(deduped[0]["chunk_idx"], 0)
        self.assertEqual(deduped[1]["chunk_idx"], 1)

    def test_reranker_and_evidence_filter(self):
        query = "annual leave policy days"
        candidates = [
            {"source_id": 1, "chunk_idx": 0, "text": "Annual leave policy gives 20 days per calendar year.", "score": 0.8},
            {"source_id": 1, "chunk_idx": 1, "text": "Completely unrelated text about printer network configuration.", "score": 0.1},
        ]
        reranked = rerank_and_filter(query, candidates, top_k=5, min_evidence_score=0.30)
        # Unrelated text should be filtered out
        self.assertEqual(len(reranked), 1)
        self.assertEqual(reranked[0]["chunk_idx"], 0)
        self.assertGreater(reranked[0]["final_score"], 0.4)

    def test_fetch_company_knowledge_with_okf_and_provenance(self):
        sample_doc = "Alpha Team leads Project Phoenix.\n"
        index_okf_source(9999, sample_doc, [sample_doc])

        mock_hits = [
            {"source_id": 9999, "chunk_idx": 0, "title": "Phoenix Spec", "text": "Alpha Team leads Project Phoenix.", "score": 0.85}
        ]

        with patch("core.knowledge_router.search_knowledge_hybrid", return_value=mock_hits), \
             patch("core.knowledge_router.ensure_knowledge_vectors", return_value=None):
            hits, prompt_block = asyncio.run(fetch_company_knowledge("Who leads Project Phoenix?", allowed_source_ids={9999}))
            self.assertTrue(len(hits) > 0)
            self.assertIn("ORGANIZATIONAL KNOWLEDGE BASE", prompt_block)
            self.assertIn("Structured Entity Relationships (OKF)", prompt_block)
            self.assertIn("Alpha Team", prompt_block)
            self.assertIn("Project Phoenix", prompt_block)
            self.assertIn("Chunk 0", prompt_block)

    def test_determine_retrieval_scope(self):
        from core.knowledge_router import determine_retrieval_scope
        # Structural / relational query
        s1 = determine_retrieval_scope("who reports to Alice in the org chart?")
        self.assertEqual(s1["scope"], "structured")
        self.assertTrue(s1["needs_okf"])

        # Exact symbolic query
        s2 = determine_retrieval_scope("where is ERR_VULKAN_TIMEOUT_404 defined?")
        self.assertEqual(s2["scope"], "symbolic")
        self.assertGreater(s2["weights"]["bm25"], s2["weights"]["vec"])

        # General semantic query
        s3 = determine_retrieval_scope("how does the annual review process work?")
        self.assertEqual(s3["scope"], "semantic")
        self.assertGreater(s3["weights"]["vec"], s3["weights"]["okf"])


if __name__ == "__main__":
    unittest.main()
