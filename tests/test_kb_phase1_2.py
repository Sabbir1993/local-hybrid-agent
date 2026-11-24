"""Tests for the restored fast Knowledge Base pipeline:
1. Single unified cache with float16 vector matrix (50% vector RAM reduction).
2. Standard 900-char chunking in _kb_rows.
3. Cross-Encoder reranking with heuristic fallback.
"""

from unittest import mock

import numpy as np
import pytest

import core.memory as mem
from core.memory import cache as mem_cache
from core.memory import indexing as mem_idx
from core.memory import reranker as mem_rerank
from core.memory import search as mem_search
from core.memory import store as mem_store


@pytest.fixture
def isolated_memory_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test_memory.db"
    monkeypatch.setattr(mem, "MEMORY_DB_FILE", db_path)
    monkeypatch.setattr(mem_store, "MEMORY_DB_FILE", db_path)
    monkeypatch.setattr(mem, "_conn", None)
    monkeypatch.setattr(mem_store, "_conn", None)
    monkeypatch.setattr(mem_cache, "_cache", {"gen": None})
    yield db_path
    conn = getattr(mem_store, "_conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    monkeypatch.setattr(mem, "_conn", None)
    monkeypatch.setattr(mem_store, "_conn", None)


def test_single_cache_and_float16_matrix(isolated_memory_db):
    v1 = [1.0, 0.0, 0.0, 0.0]
    mem_store._store_vecs([("knowledge", "kb:10", 0, "Bangladesh Bank circular on NPL.", 1.0)], [v1])

    c1 = mem_cache._cached_entries()
    assert c1["mat"].dtype == np.float16
    assert len(c1["entries"]) == 1

    cos = mem_cache._cosines(c1, [0], [1.0, 0.0, 0.0, 0.0])
    assert abs(cos[0] - 1.0) < 1e-3


def test_kb_rows_standard_chunking():
    long_body = "\n".join(f"Clause {i}: Single borrower exposure limit is 15% of capital." for i in range(25))
    rows = mem_idx._kb_rows("kb:5", long_body, 1.0)
    assert len(rows) >= 2
    assert all(len(r) == 5 for r in rows)


def test_cross_encoder_rerank_and_fallback():
    cands = [
        {"source": "knowledge", "path": "kb:1", "source_id": 1, "text": "General office guidelines.", "score": 0.7},
        {"source": "knowledge", "path": "kb:2", "source_id": 2, "text": "DSCR minimum ratio is 1.25x for corporate loans.", "score": 0.5},
    ]
    with mock.patch("core.memory.cross_encoder.score_pairs", return_value=[0.1, 0.95]):
        ranked = mem_rerank.rerank_and_filter("repayment capacity ratio", cands, top_k=2, min_evidence_score=0.30)
        assert len(ranked) == 1
        assert "DSCR" in ranked[0]["text"]

    with mock.patch("core.memory.cross_encoder.score_pairs", return_value=None):
        ranked_fb = mem_rerank.rerank_and_filter("DSCR ratio", cands, top_k=2, min_evidence_score=0.20)
        assert len(ranked_fb) >= 1
        assert "DSCR" in ranked_fb[0]["text"]
