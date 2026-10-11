import unittest
from unittest import mock

from core import knowledge_access
from core.memory import constants, indexing


def row(i, title, indexed, total):
    return {"id": i, "title": title, "chunks_indexed": indexed, "chunks_total": total}


class ChunkCapTests(unittest.TestCase):
    def test_default_cap_is_far_above_the_old_400(self):
        self.assertGreaterEqual(constants.MAX_KB_CHUNKS_PER_DOC, 10000)

    def test_cap_is_configurable_and_bounded(self):
        with mock.patch.dict("core.small_model.APP_CONFIG", {"knowledge": {"max_chunks_per_doc": 5}}):
            self.assertEqual(constants.kb_chunk_cap(), 5)
        with mock.patch.dict("core.small_model.APP_CONFIG", {"knowledge": {"max_chunks_per_doc": 10 ** 9}}):
            self.assertEqual(constants.kb_chunk_cap(), 200000)
        with mock.patch.dict("core.small_model.APP_CONFIG", {"knowledge": {"max_chunks_per_doc": "junk"}}):
            self.assertEqual(constants.kb_chunk_cap(), constants.MAX_KB_CHUNKS_PER_DOC)

    def test_report_marks_a_document_cut_at_the_cap(self):
        text = "\n".join(f"row {i} " + "x" * 80 for i in range(400))      # well over 3 chunks
        with mock.patch.dict("core.small_model.APP_CONFIG", {"knowledge": {"max_chunks_per_doc": 3}}):
            rep = indexing.kb_chunk_report(text)
        self.assertEqual(rep["indexed"], 3)
        self.assertGreater(rep["total"], 3)

    def test_report_for_a_small_document_is_complete(self):
        rep = indexing.kb_chunk_report("short text")
        self.assertEqual(rep, {"indexed": 1, "total": 1})


class PartialNoticeTests(unittest.TestCase):
    def _notice(self, rows, ids):
        with mock.patch.object(knowledge_access.auth_db, "list_knowledge_sources", return_value=rows):
            return knowledge_access.partial_notice(ids)

    def test_names_only_partial_sources_the_user_can_read(self):
        rows = [row(1, "Big roster", 20000, 90000), row(2, "Policy", 10, 10), row(3, "Hidden", 1, 9)]
        note = self._notice(rows, {1, 2})
        self.assertIn("Big roster", note)
        self.assertNotIn("Policy", note)
        self.assertNotIn("Hidden", note)

    def test_complete_or_unknown_sources_give_no_notice(self):
        self.assertEqual(self._notice([row(1, "A", 5, 5), row(2, "Old", None, None)], {1, 2}), "")


if __name__ == "__main__":
    unittest.main()
