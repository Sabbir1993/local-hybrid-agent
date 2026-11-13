"""tests/test_kb_min_evidence.py - knowledge.min_evidence reaches rerank_and_filter (default 0.30, clamped 0-1).

Run: python -m unittest tests.test_kb_min_evidence -v
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import knowledge_router as kr  # noqa: E402
from core.small_model import APP_CONFIG  # noqa: E402


class MinEvidenceParseTests(unittest.TestCase):
    def _with(self, value):
        cfg = APP_CONFIG.get("knowledge")
        APP_CONFIG["knowledge"] = {} if value is None else {"min_evidence": value}
        try:
            return kr.min_evidence_score()
        finally:
            if cfg is None:
                APP_CONFIG.pop("knowledge", None)
            else:
                APP_CONFIG["knowledge"] = cfg

    def test_default_when_unset(self):
        self.assertEqual(self._with(None), 0.30)

    def test_configured_value_is_used(self):
        self.assertEqual(self._with(0.35), 0.35)

    def test_clamped_to_unit_interval(self):
        self.assertEqual(self._with(-1), 0.0)
        self.assertEqual(self._with(7), 1.0)

    def test_non_number_falls_back_to_default(self):
        self.assertEqual(self._with("high"), 0.30)
        self.assertEqual(self._with([0.4]), 0.30)


class MinEvidenceFlowTests(unittest.TestCase):
    def setUp(self):
        self._cfg = APP_CONFIG.get("knowledge")

    def tearDown(self):
        if self._cfg is None:
            APP_CONFIG.pop("knowledge", None)
        else:
            APP_CONFIG["knowledge"] = self._cfg

    def _fetch(self, knowledge_cfg):
        APP_CONFIG["knowledge"] = knowledge_cfg
        rerank = mock.Mock(return_value=[])
        with (mock.patch.object(kr, "ensure_knowledge_vectors", mock.AsyncMock()),
              mock.patch.object(kr, "ensure_okf_indexed"),
              mock.patch.object(kr, "search_knowledge_hybrid", mock.AsyncMock(return_value=[])),
              mock.patch.object(kr, "search_okf", return_value=[]),
              mock.patch.object(kr, "rerank_and_filter", rerank)):
            asyncio.run(kr.fetch_company_knowledge("bfsi report", {1}, k=6))
        return rerank

    def test_configured_value_reaches_rerank_and_filter(self):
        rerank = self._fetch({"min_evidence": 0.40})
        self.assertEqual(rerank.call_args.kwargs.get("min_evidence_score"), 0.40)

    def test_default_reaches_rerank_and_filter_when_unset(self):
        rerank = self._fetch({})
        self.assertEqual(rerank.call_args.kwargs.get("min_evidence_score"), 0.30)


if __name__ == "__main__":
    unittest.main()
