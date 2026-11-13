"""tests/test_reranker_coverage.py - coverage term ignores filler words (same normalisation as lexical search).

Run: python -m unittest tests.test_reranker_coverage -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.memory.reranker import calculate_cross_features, rerank_and_filter  # noqa: E402

Q = "I need the detailed report of 4th november of BFSI team"
FILLER = "the report of need is here"
RELEVANT = "BFSI team November report pipeline numbers"
EVIDENCE_FLOOR = 0.30


class CoverageNormalisationTests(unittest.TestCase):
    def test_filler_only_chunk_is_below_evidence_floor(self):
        self.assertLess(calculate_cross_features(Q, FILLER, base_score=0.35), EVIDENCE_FLOOR)

    def test_relevant_chunk_passes_and_beats_filler(self):
        rel = calculate_cross_features(Q, RELEVANT, base_score=0.35)
        fil = calculate_cross_features(Q, FILLER, base_score=0.35)
        self.assertGreaterEqual(rel, EVIDENCE_FLOOR)
        self.assertGreater(rel, fil)

    def test_rerank_drops_filler_and_keeps_relevant(self):
        cands = [{"text": FILLER, "score": 0.35, "source_id": 1, "chunk_idx": 0},
                 {"text": RELEVANT, "score": 0.35, "source_id": 2, "chunk_idx": 0}]
        out = rerank_and_filter(Q, cands, top_k=6, min_evidence_score=EVIDENCE_FLOOR)
        self.assertEqual([c["source_id"] for c in out], [2])

    def test_stopword_only_query_does_not_crash(self):
        for q in ("the of and is", "a", "I", "the the the"):
            s = calculate_cross_features(q, "the report of the team", base_score=0.4)
            self.assertGreaterEqual(s, 0.0)
            self.assertLessEqual(s, 1.0)

    def test_empty_query_or_text_returns_zero(self):
        self.assertEqual(calculate_cross_features("", "text"), 0.0)
        self.assertEqual(calculate_cross_features("query words", ""), 0.0)


if __name__ == "__main__":
    unittest.main()
