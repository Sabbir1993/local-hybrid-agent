import asyncio
import sys
import time
import unittest
from unittest import mock

from core import kb_queue
from core.memory import cache as cache_mod
from tests.test_knowledge_recall import _TempDb
from tests.test_knowledge_routes import _KbApp


def reset_queue():
    kb_queue._jobs.clear()
    kb_queue._queue = kb_queue._worker = kb_queue._loop = None


class QueueTests(unittest.TestCase):
    def setUp(self):
        reset_queue()

    def test_jobs_run_in_order_with_progress_and_a_failure_does_not_stop_the_queue(self):
        order, seen = [], []

        async def fake_finish(sid, text, user, progress=None):
            progress(1, 2)
            seen.append(dict(kb_queue.progress(sid)))
            progress(2, 2)
            order.append((sid, text))
            return {"ok": True}

        def loader(text):
            async def load():
                return text
            return load

        async def bad():
            raise ValueError("cannot read this file")

        async def go():
            with mock.patch("routes.knowledge.helpers._finish_ingest", fake_finish), \
                    mock.patch("core.auth_db.update_knowledge_source_status") as status:
                await kb_queue.enqueue(1, loader("a"), None)
                await kb_queue.enqueue(2, bad, None)
                await kb_queue.enqueue(3, loader("c"), None)
                self.assertTrue(kb_queue.active())
                await kb_queue._queue.join()
                return status

        status = asyncio.run(go())
        self.assertEqual(order, [(1, "a"), (3, "c")])
        self.assertEqual(seen[0]["state"], "indexing")
        self.assertEqual(kb_queue.progress(1)["state"], "done")
        self.assertEqual(kb_queue.progress(2)["state"], "error")
        status.assert_called_once()
        self.assertIn("cannot read this file", status.call_args[0][2])
        self.assertFalse(kb_queue.active())

    def test_a_failed_ingest_result_is_an_error_state(self):
        async def fake_finish(sid, text, user, progress=None):
            return {"ok": False, "error": "full"}

        async def load():
            return "x"

        async def go():
            with mock.patch("routes.knowledge.helpers._finish_ingest", fake_finish):
                await kb_queue.enqueue(9, load, None)
                await kb_queue._queue.join()
        asyncio.run(go())
        self.assertEqual(kb_queue.progress(9)["state"], "error")

    def test_old_finished_jobs_are_pruned(self):
        for i in range(210):
            kb_queue._jobs[i] = {"state": "done", "done": 0, "total": 0, "finished": float(i)}
        kb_queue._prune()
        self.assertEqual(len(kb_queue._jobs), 200)
        self.assertNotIn(0, kb_queue._jobs)


class OrphanTests(unittest.TestCase):
    def test_processing_source_without_a_job_is_marked_interrupted(self):
        rows = [{"id": 1, "status": "processing", "updated_at": time.time() - 3600},
                {"id": 2, "status": "processing", "updated_at": time.time()},
                {"id": 3, "status": "ready", "updated_at": 0}]
        reset_queue()
        with mock.patch("core.auth_db.list_knowledge_sources", return_value=rows), \
                mock.patch("core.auth_db.update_knowledge_source_status") as status:
            self.assertEqual(kb_queue.sweep_orphans(), 1)
        self.assertEqual(status.call_args[0][:2], (1, "error"))


class CacheThrottleTests(unittest.TestCase):
    def _db(self):
        fake_db = mock.Mock()
        fake_db.execute.return_value.fetchall.return_value = []
        return fake_db

    def test_stale_cache_is_served_during_a_bulk_ingest_then_rebuilt(self):
        stale = {"gen": "1", "built_at": time.time(), "entries": [("knowledge", "kb:1", "t", None)]}
        with mock.patch.object(cache_mod, "_cache", stale), mock.patch.object(cache_mod, "_chunks_gen", return_value="2"), \
                mock.patch.object(cache_mod, "_ingest_active", return_value=True):
            self.assertIs(cache_mod._cached_entries(), stale)
        old = dict(stale, built_at=time.time() - 3600)
        with mock.patch.object(cache_mod, "_cache", old), mock.patch.object(cache_mod, "_chunks_gen", return_value="2"), \
                mock.patch.object(cache_mod, "_ingest_active", return_value=True), \
                mock.patch.object(cache_mod, "_db", return_value=self._db()), mock.patch.object(cache_mod, "_set_cache"):
            self.assertIsNot(cache_mod._cached_entries(), old)           # too old: rebuilt even while ingesting

    def test_no_throttle_when_nothing_is_ingesting(self):
        stale = {"gen": "1", "built_at": time.time(), "entries": [("knowledge", "kb:1", "t", None)]}
        with mock.patch.object(cache_mod, "_cache", stale), mock.patch.object(cache_mod, "_chunks_gen", return_value="2"), \
                mock.patch.object(cache_mod, "_ingest_active", return_value=False), \
                mock.patch.object(cache_mod, "_db", return_value=self._db()), mock.patch.object(cache_mod, "_set_cache"):
            self.assertIsNot(cache_mod._cached_entries(), stale)


class ProgressIndexingTests(_TempDb):
    def test_embedding_reports_progress_in_groups(self):
        import core.memory  # noqa: F401
        memory_pkg = sys.modules["core.memory"]
        from core.memory import indexing
        calls = []

        async def embed(texts):
            calls.append(len(texts))
            return [[0.1, 0.2]] * len(texts)
        text = "\n\n".join(f"Section {i}. " + "word " * 160 for i in range(60))
        reports = []
        with mock.patch.object(memory_pkg, "_embed_texts", embed), mock.patch.object(indexing, "KB_EMBED_GROUP", 20):
            n = asyncio.run(indexing.index_knowledge_source(5, text, progress=lambda d, t: reports.append((d, t))))
        self.assertGreater(n, 20)
        self.assertEqual(reports[-1], (n, n))
        self.assertTrue(all(c <= 20 for c in calls) and len(calls) >= 2)

    def test_embedder_down_still_indexes_lexically(self):
        import core.memory  # noqa: F401
        memory_pkg = sys.modules["core.memory"]
        from core.memory import indexing

        async def down(texts):
            return None
        with mock.patch.object(memory_pkg, "_embed_texts", down):
            n = asyncio.run(indexing.index_knowledge_source(6, "alpha beta " * 400, progress=lambda d, t: None))
        self.assertGreater(n, 0)


class IndexFullGuardTests(unittest.TestCase):
    def test_ingest_is_refused_when_the_index_would_exceed_the_limit(self):
        from routes.knowledge import helpers
        with mock.patch.object(helpers, "kb_total_chunk_limit", return_value=1000), \
                mock.patch.object(helpers, "kb_chunk_count", return_value=990), \
                mock.patch.object(helpers.auth_db, "update_knowledge_source_status") as status, \
                mock.patch.object(helpers, "audit_log"):
            res = asyncio.run(helpers._finish_ingest(1, "word " * 20000, None))
        self.assertFalse(res["ok"])
        self.assertIn("max_total_chunks", res["error"])
        self.assertEqual(status.call_args[0][1], "error")


class UploadRoutingTests(_KbApp):
    @staticmethod
    def _file(name, data: bytes):
        import io
        return {"file": (name, io.BytesIO(data), "application/octet-stream")}

    def test_big_upload_is_queued_and_returns_immediately(self):
        calls = []

        async def fake_enqueue(sid, load, user):
            calls.append(sid)
        with mock.patch.object(kb_queue, "background_min_bytes", return_value=5), \
                mock.patch.object(kb_queue, "enqueue", fake_enqueue):
            r = self.client.post("/knowledge/upload", files=self._file("a.csv", b"x" * 50), data={"title": "big"})
        self.assertEqual(r.status_code, 200, r.text[:200])
        self.assertTrue(r.json()["queued"])
        self.assertEqual(r.json()["source"]["status"], "processing")
        self.assertEqual(len(calls), 1)

    def test_small_upload_still_ingests_inline(self):
        r = self.client.post("/knowledge/upload", files=self._file("a.csv", b"hi"), data={"title": "small"})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("queued", r.json())

    def test_stats_endpoint(self):
        r = self.client.get("/knowledge/stats")
        self.assertEqual(r.status_code, 200, r.text[:200])
        self.assertIn("chunk_limit", r.json())


if __name__ == "__main__":
    unittest.main()
