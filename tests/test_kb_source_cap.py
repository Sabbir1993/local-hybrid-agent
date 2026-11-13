"""tests/test_kb_source_cap.py - at most knowledge.max_chunks_per_source chunks per document, backfilled to k.

Run: python -m unittest tests.test_kb_source_cap -v
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import knowledge_router as kr  # noqa: E402
from core.small_model import APP_CONFIG  # noqa: E402


def _c(sid, n, score):
    return {"source_id": sid, "title": f"Doc {sid}", "chunk_idx": n, "text": f"doc{sid} chunk{n}", "score": score}


class SourceCapTests(unittest.TestCase):
    def setUp(self):
        self._cfg = APP_CONFIG.get("knowledge")

    def tearDown(self):
        if self._cfg is None:
            APP_CONFIG.pop("knowledge", None)
        else:
            APP_CONFIG["knowledge"] = self._cfg

    def _fetch(self, ranked, knowledge_cfg, k=6):
        APP_CONFIG["knowledge"] = knowledge_cfg
        seen = {}

        def fake_rerank(query, cands, top_k=6, min_evidence_score=0.30, okf_facts=None):
            seen["top_k"] = top_k
            return [dict(c) for c in ranked[:top_k]]

        with (mock.patch.object(kr, "ensure_knowledge_vectors", mock.AsyncMock()),
              mock.patch.object(kr, "ensure_okf_indexed"),
              mock.patch.object(kr, "search_knowledge_hybrid", mock.AsyncMock(return_value=[])),
              mock.patch.object(kr, "search_okf", return_value=[]),
              mock.patch.object(kr, "rerank_and_filter", side_effect=fake_rerank)):
            hits, _ = asyncio.run(kr.fetch_company_knowledge("doc question", {1, 2}, k=k))
        return hits, seen["top_k"]

    def test_cap_then_backfill_keeps_score_order(self):
        # 6 strong chunks from A, 2 weaker from B: 3 A kept, 2 B kept, 1 A backfilled (best skipped first)
        ranked = [_c(1, i, 0.9 - i * 0.01) for i in range(6)] + [_c(2, 0, 0.7), _c(2, 1, 0.6)]
        hits, top_k = self._fetch(ranked, {"max_chunks_per_source": 3})
        self.assertEqual(top_k, 12)
        self.assertEqual([(h["source_id"], h["chunk_idx"]) for h in hits],
                         [(1, 0), (1, 1), (1, 2), (1, 3), (2, 0), (2, 1)])

    def test_only_one_source_still_returns_k(self):
        ranked = [_c(1, i, 0.9 - i * 0.01) for i in range(8)]
        hits, _ = self._fetch(ranked, {"max_chunks_per_source": 3})
        self.assertEqual([h["source_id"] for h in hits], [1] * 6)
        self.assertEqual([h["chunk_idx"] for h in hits], [0, 1, 2, 3, 4, 5])

    def test_cap_disabled_keeps_previous_behaviour(self):
        ranked = [_c(1, i, 0.9 - i * 0.01) for i in range(6)] + [_c(2, 0, 0.7), _c(2, 1, 0.6)]
        hits, top_k = self._fetch(ranked, {"max_chunks_per_source": 0})
        self.assertEqual(top_k, 6)
        self.assertEqual([h["chunk_idx"] for h in hits], [0, 1, 2, 3, 4, 5])
        self.assertTrue(all(h["source_id"] == 1 for h in hits))


if __name__ == "__main__":
    unittest.main()
