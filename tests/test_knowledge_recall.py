"""tests/test_knowledge_recall.py - E1: KB chunk-cap truncation + per-caller caps.

A 900-char/150-overlap chunker with a shared 40-chunk cap silently drops every
KB document past ~30KB -- and company knowledge arrives as long PDFs. The cap
must be per-caller (KB corpora are small and precious; workspace/session caps
stay where they are), and the tail of a long doc must be retrievable.

Run: python -m unittest tests.test_knowledge_recall -v
"""

import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import auth_db
from core.memory import indexing as indexing_mod
from core.memory import search as search_mod
from core.memory import constants as mem_const
from core.memory.indexing import MAX_FILE_BYTES


def _long_doc(n_chunks: int) -> str:
    # ~800 chars per section: 60 sections ~= 48KB ~= 64 chunks uncapped, so the
    # shared 40-cap genuinely drops the tail (a 30KB doc would fit and prove nothing).
    # The last section is lexically distinctive (teleportation reimbursement):
    # repetitive filler ties every chunk's score, so only a distinctive tail can
    # prove it was indexed at all rather than outranked.
    parts = []
    for i in range(n_chunks):
        body = (f"Section {i:03d} of the employee handbook. " * 20).strip()
        parts.append(body + f" Unique marker alpha-{i:03d}-zulu.")
    parts.append(
        "Teleportation reimbursement policy. Employees who commute by quantum teleportation "
        "may claim starship mileage at the approved astral rate. File form Q-9 with the "
        "ministry of unremarkable travel within three lunar cycles. Unique marker alpha-999-zulu.")
    return "\n\n".join(parts)


class _TempDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        import core.memory as memory_pkg
        from core.memory import cache as cache_mod
        from core.memory import store as store_mod
        self._saved_mem_conn = store_mod._conn
        self._saved_pkg_conn = getattr(memory_pkg, "_conn", None)
        self._had_mem_file = hasattr(memory_pkg, "MEMORY_DB_FILE")
        self._saved_mem_file = getattr(memory_pkg, "MEMORY_DB_FILE", None)
        store_mod._conn = None
        memory_pkg._conn = None
        memory_pkg.MEMORY_DB_FILE = Path(self.tmp.name) / "memory.db"
        store_mod._db()  # creates schema + triggers on the temp file
        cache_mod._set_cache({"gen": None, "entries": [], "lower": [], "dim": None, "mat": None, "pos": None})
        self._saved_auth = auth_db._auth_db
        with mock.patch.object(auth_db, "AUTH_DB_FILE", Path(self.tmp.name) / "auth.db"):
            auth_db._auth_db = auth_db._init_auth_db()

    def tearDown(self):
        import core.memory as memory_pkg
        from core.memory import cache as cache_mod
        from core.memory import store as store_mod
        try:
            store_mod._db().close()
        except Exception:
            pass
        store_mod._conn = self._saved_mem_conn
        memory_pkg._conn = self._saved_pkg_conn
        if self._had_mem_file:
            memory_pkg.MEMORY_DB_FILE = self._saved_mem_file
        elif hasattr(memory_pkg, "MEMORY_DB_FILE"):
            delattr(memory_pkg, "MEMORY_DB_FILE")
        cache_mod._set_cache({"gen": None, "entries": [], "lower": [], "dim": None, "mat": None, "pos": None})
        auth_db._auth_db.close()
        auth_db._auth_db = self._saved_auth
        self.tmp.cleanup()


class KbChunkCapTests(_TempDb):
    def _index(self, text):
        import core.memory as memory_pkg

        async def no_embed(texts_in):
            return None
        with mock.patch.object(memory_pkg, "_embed_texts", no_embed):
            return asyncio.run(indexing_mod.index_knowledge_source(7, text))

    def test_long_kb_doc_is_fully_indexed(self):
        n = self._index(_long_doc(60))
        self.assertGreater(n, 40, f"only {n} chunks indexed - the tail is invisible to recall")

    def test_tail_chunk_is_retrievable(self):
        self._index(_long_doc(60))
        from core.auth_db import knowledge_acl
        sid = knowledge_acl.create_knowledge_source("handbook", "text", "t")
        knowledge_acl.update_knowledge_source_status(sid, "ready")
        # re-index under the real source id so the ACL filter passes
        import core.memory as memory_pkg

        async def no_embed(texts_in):
            return None
        with mock.patch.object(memory_pkg, "_embed_texts", no_embed):
            asyncio.run(indexing_mod.index_knowledge_source(sid, _long_doc(60)))

        async def ask(q):
            with mock.patch.object(memory_pkg, "_embed_texts", no_embed):
                return await search_mod.search_knowledge_hybrid(
                    q, k=6, allowed_knowledge_source_ids={sid})
        hits = asyncio.run(ask("teleportation reimbursement form Q-9 astral rate"))
        self.assertTrue(any("alpha-999-zulu" in h["text"] for h in hits),
                        "tail chunk missing from results")

    def test_workspace_and_session_caps_unchanged(self):
        # per-caller caps must not lift the workspace/session limits as a side
        # effect: those bound index size and agent context, and F decides them.
        self.assertEqual(mem_const.MAX_CHUNKS_PER_DOC, 40)


class RealVecCacheTests(unittest.TestCase):
    """R1: the committed nomic vector cache covers the eval corpus exactly.

    No GPU, no server: this guards the cache against drift (new question added
    to eval_mock without re-running scripts/embed_eval_corpus.py) and against
    chunk-param changes (R5) silently measuring stale-boundary vectors.
    """

    def test_cache_covers_corpus_and_chunk_params(self):
        import hashlib
        import json
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from eval_mock import RETRIEVAL_DOCS, RETRIEVAL_QUESTIONS
        cache_file = Path(__file__).resolve().parent / ".eval_vec_cache.json"
        self.assertTrue(cache_file.exists(),
                        "cache missing - boot :8093 and run scripts/embed_eval_corpus.py")
        cache = json.loads(cache_file.read_text(encoding="utf-8"))
        self.assertEqual(cache.get("chunk_chars"), mem_const.CHUNK_CHARS)
        self.assertEqual(cache.get("chunk_overlap"), mem_const.CHUNK_OVERLAP)
        self.assertEqual(cache.get("chunk_snap"), mem_const.CHUNK_SNAP)
        dim = cache.get("dim")
        self.assertTrue(isinstance(dim, int) and dim > 0)
        vecs = cache.get("vecs") or {}
        want = set()
        for _, body in RETRIEVAL_DOCS:
            want.update(mem_const._chunk_text(body))
        want.update(q for q, _, _ in RETRIEVAL_QUESTIONS)
        missing = [t[:40] for t in want
                   if hashlib.sha1(t.encode()).hexdigest() not in vecs]
        self.assertFalse(missing, f"cache stale, missing {len(missing)}: {missing[:2]}")
        for t in want:
            v = vecs[hashlib.sha1(t.encode()).hexdigest()]
            self.assertEqual(len(v), dim, "ragged vector in cache")


class LexicalNormalizeTests(unittest.TestCase):
    """R3 RED: query normalization for the lexical scorer.

    The scorer counts raw query words (`core/memory/search.py`): stopwords
    ("are", "how", "do" ...) inflate every score equally, and unstemmed
    plurals ("receipts" vs doc "receipt") miss entirely. Both sides must
    normalize identically (query words, title words) because matching is
    substring counting - stemmed query "receipt" still matches doc "receipt".
    """

    def test_stemmer(self):
        self.assertEqual(mem_const._stem_word("receipts"), "receipt")
        self.assertEqual(mem_const._stem_word("days"), "day")
        self.assertEqual(mem_const._stem_word("requests"), "request")
        self.assertEqual(mem_const._stem_word("returns"), "return")
        self.assertEqual(mem_const._stem_word("policies"), "policy")
        self.assertEqual(mem_const._stem_word("needed"), "need")
        self.assertEqual(mem_const._stem_word("accepted"), "accept")
        self.assertEqual(mem_const._stem_word("running"), "run")
        # guards: short words and ss-endings are not word salad
        self.assertEqual(mem_const._stem_word("bus"), "bus")
        self.assertEqual(mem_const._stem_word("class"), "class")
        self.assertEqual(mem_const._stem_word("need"), "need")

    def test_stopwords(self):
        for w in ("are", "how", "do", "does", "the", "what", "when", "where",
                  "which", "who", "many", "much", "can", "should", "with",
                  "for", "and", "you", "your", "our", "all"):
            self.assertIn(w, mem_const.STOPWORDS, w)
        # content nouns stay, however frequent - frequency is the ranker's job
        for w in ("receipt", "refund", "deploy", "roll", "vacation", "days",
                  "ship", "sick", "staging", "secondary"):
            self.assertNotIn(w, mem_const.STOPWORDS, w)


def _para_doc(n_paras: int = 12, para_len: int = 400) -> str:
    words = ("alpha beta gamma delta handbook policy procedure manual "
             "section item clause provision article").split()
    paras = []
    for i in range(n_paras):
        body = " ".join(words[(i + j) % len(words)] for j in range(para_len // 6))
        paras.append(f"Paragraph {i:03d}. {body}.")
    return "\n\n".join(paras)


class ChunkSplitterTests(unittest.TestCase):
    """R5: the splitter covers, terminates, and (snapped) respects boundaries."""

    def test_legacy_path_covers_and_terminates(self):
        doc = _para_doc()
        chunks = mem_const._chunk_text(doc)
        self.assertGreater(len(chunks), 1)
        # tail reaches the end; every chunk non-trivial
        self.assertTrue(doc.endswith(chunks[-1][-50:]))
        self.assertTrue(all(len(c) > 100 for c in chunks))
        # overlap: consecutive chunks share text
        self.assertIn(chunks[0][-50:], chunks[1][:200])

    def test_max_chunks_respected(self):
        doc = _para_doc(n_paras=200)
        self.assertLessEqual(len(mem_const._chunk_text(doc, 5)), 5)

    def test_snapped_cuts_land_on_boundaries(self):
        doc = _para_doc()
        with mock.patch.object(mem_const, "CHUNK_SNAP", True):
            chunks = mem_const._chunk_text(doc)
        self.assertGreater(len(chunks), 1)
        # Product property, not positions: no word is split mid-chunk in a way
        # that loses it - every source word survives whole in some chunk.
        for w in {w for p in doc.split("\n\n") for w in p.split()}:
            self.assertTrue(any(w in c for c in chunks), f"lost word: {w!r}")
        self.assertTrue(doc.endswith(chunks[-1][-50:]))

    def test_snap_cut_unit(self):
        text = ("word " * 30) + "AAA BBB\n\nCCC DDD EEE" + (" tail" * 30)
        # prefers the paragraph break over nearer spaces/newlines
        self.assertEqual(mem_const._snap_cut(text, 0, len(text) - 1),
                         text.index("\n\n") + 2)
        # hard cut when nothing past the floor; progress always
        self.assertEqual(mem_const._snap_cut("a" * 500, 0, 400), 400)
        self.assertGreater(mem_const._snap_cut(text, 0, 5), 0)

    def test_snapped_terminates_without_any_spaces(self):
        doc = "a" * 5000  # adversarial: no boundary anywhere
        with mock.patch.object(mem_const, "CHUNK_SNAP", True):
            chunks = mem_const._chunk_text(doc)
        self.assertGreater(len(chunks), 1)
        joined_len = sum(len(c) for c in chunks)
        self.assertGreaterEqual(joined_len, len(doc))

    def test_snapped_keeps_short_docs_whole(self):
        doc = "short doc"
        with mock.patch.object(mem_const, "CHUNK_SNAP", True):
            self.assertEqual(mem_const._chunk_text(doc), [doc])


class ChunkBoundaryRetrievalTests(_TempDb):
    """R5 permanent pin: snapped boundaries keep markers retrievable.

    The R5 real-vector validation showed 900/150+snap at recall 1.0 vs 0.833
    unsnapped (a boundary-split marker falls below the min_cos gate). This
    compacts that finding into a fast lexical regression test: two marker
    sections placed to straddle the 900-char cut unsnapped must both retrieve
    with snap on. If CHUNK_SNAP ever flips off, this fails first.
    """

    _DOC = ("\n\n".join(
        ["Handbook filler about expense reports and travel bookings. " * 8] * 2
        + ["Marker QUASAR section. The QUASAR protocol requires tungsten invoices."]
        + ["Handbook filler about meeting rooms and visitor badges. " * 8] * 3
        + ["Marker VORTEX section. The VORTEX rotation pages engineers by pager."]
        + ["Handbook filler about parking validation and front desk hours. " * 8] * 2))

    def _ask(self, q):
        import core.memory as memory_pkg

        async def no_embed(texts_in):
            return None
        from core.auth_db import knowledge_acl
        sid = knowledge_acl.create_knowledge_source("handbook", "text", "t")
        knowledge_acl.update_knowledge_source_status(sid, "ready")
        with mock.patch.object(memory_pkg, "_embed_texts", no_embed):
            asyncio.run(indexing_mod.index_knowledge_source(sid, self._DOC))
            return asyncio.run(search_mod.search_knowledge_hybrid(
                q, k=6, allowed_knowledge_source_ids={sid}))

    def test_snapped_markers_retrieve(self):
        self.assertTrue(mem_const.CHUNK_SNAP, "R5 winner flipped off?")
        for q, word in (("what does the quasar protocol require?", "quasar"),
                        ("how does the vortex rotation page?", "vortex")):
            hits = self._ask(q)
            self.assertTrue(any(word in h["text"].lower() for h in hits),
                            f"marker {word!r} lost at a chunk boundary")


class RerankTests(_TempDb):
    """R6 RED: second-stage ordering for the KB path.

    The web path fuses engine ranks + cosine + lexical via RRF
    (`core/web_search/rerank.py:66`); the KB path stops at a one-shot weighted
    sum. A rerank pass reorders a broad candidate set with a different signal
    (rank fusion over cosine/lexical/title orderings) instead of rescoring
    with the same weights - that is what makes it a second opinion rather
    than a reheated first one. No fresh embeddings: query vector + stored
    chunk vecs are reused, so the pass costs microseconds, not a GPU round.
    """

    _DOCS = {
        "handbook": ("The QUASAR protocol requires tungsten invoices. " * 3),
        "archive": ("Tungsten invoices are processed quarterly. " * 3
                    + "The travel protocol requires approval. " * 3),
    }

    def _ask(self, **kw):
        import core.memory as memory_pkg

        async def no_embed(texts_in):
            return None
        from core.auth_db import knowledge_acl
        allowed = set()
        with mock.patch.object(memory_pkg, "_embed_texts", no_embed):
            for title, body in self._DOCS.items():
                sid = knowledge_acl.create_knowledge_source(title, "text", "t")
                knowledge_acl.update_knowledge_source_status(sid, "ready")
                allowed.add(sid)
                asyncio.run(indexing_mod.index_knowledge_source(sid, body))
            return asyncio.run(search_mod.search_knowledge_hybrid(
                "what does the quasar protocol require?", k=2,
                allowed_knowledge_source_ids=allowed, **kw))

    def test_rerank_returns_ranked_hits(self):
        hits = self._ask(rerank=True)
        self.assertLessEqual(len(hits), 2)
        self.assertTrue(all({"title", "text", "score"} <= set(h) for h in hits))
        self.assertTrue(any("quasar" in h["text"].lower() for h in hits),
                        "rerank buried the only relevant chunk")


class EmbedderCircuitTests(_TempDb):
    """core/memory/indexing.py _embed_texts: the embedder-down circuit breaker,
    batching and response validation. All paths must degrade to lexical-only
    (return None) rather than raise, because the caller banks on None."""

    def test_empty_input_returns_none(self):
        import core.memory as memory_pkg
        from core.memory import indexing as idx
        from core.memory import store as store_mod
        orig = idx._embedder_down_until
        try:
            idx._embedder_down_until = 0.0
            self.assertIsNone(asyncio.run(idx._embed_texts([])))
        finally:
            idx._embedder_down_until = orig

    def test_cooldown_window_returns_none_without_calling(self):
        import asyncio
        from core.memory import indexing as idx
        idx._embedder_down_until = time.time() + 60
        try:
            self.assertIsNone(asyncio.run(idx._embed_texts(["x"])))
        finally:
            idx._embedder_down_until = 0.0

    def test_unavailable_instance_sets_cooldown(self):
        import asyncio
        from core.memory import indexing as idx
        from core import lanes
        from core import small_model
        idx._embedder_down_until = 0.0
        inst = mock.Mock()
        inst.available = False
        with mock.patch.object(lanes, "targets", return_value=[mock.Mock(lane="embed")]), \
             mock.patch.object(small_model.small_models, "instances",
                               {"embed": inst}):
            self.assertIsNone(asyncio.run(idx._embed_texts(["hello world"])))
            self.assertGreater(idx._embedder_down_until, time.time())

    def test_http_error_closes_circuit(self):
        import asyncio
        from core.memory import indexing as idx
        from core import lanes
        from core import small_model
        idx._embedder_down_until = 0.0
        inst = mock.Mock()
        inst.available = True
        inst.ensure_loaded = mock.AsyncMock()

        async def boom(*a, **k):
            raise RuntimeError("embedder down")
        inst.client.post = boom
        with mock.patch.object(lanes, "targets", return_value=[mock.Mock(lane="embed")]), \
             mock.patch.object(small_model.small_models, "instances",
                               {"embed": inst}):
            self.assertIsNone(asyncio.run(idx._embed_texts(["a", "b", "c"])))
            self.assertGreater(idx._embedder_down_until, time.time())

    def test_short_response_is_rejected(self):
        import asyncio
        from core.memory import indexing as idx
        from core import lanes
        from core import small_model
        idx._embedder_down_until = 0.0
        inst = mock.Mock()
        inst.available = True
        inst.ensure_loaded = mock.AsyncMock()

        async def short(*a, **k):
            resp = mock.Mock()
            resp.raise_for_status = mock.Mock()
            resp.json.return_value = {"data": [{"embedding": [0.1]}]}   # 1 vec for 3 texts
            return resp
        inst.client.post = short
        with mock.patch.object(lanes, "targets", return_value=[mock.Mock(lane="embed")]), \
             mock.patch.object(small_model.small_models, "instances",
                               {"embed": inst}):
            self.assertIsNone(asyncio.run(idx._embed_texts(["a", "b", "c"])))

    def test_valid_batch_returns_same_count(self):
        import asyncio
        from core.memory import indexing as idx
        from core import lanes
        from core import small_model
        idx._embedder_down_until = 0.0
        inst = mock.Mock()
        inst.available = True
        inst.ensure_loaded = mock.AsyncMock()

        async def ok(*a, **k):
            resp = mock.Mock()
            resp.raise_for_status = mock.Mock()
            n = len(k["json"]["input"])
            resp.json.return_value = {"data": [{"embedding": [float(i)]} for i in range(n)]}
            return resp
        inst.client.post = ok
        with mock.patch.object(lanes, "targets", return_value=[mock.Mock(lane="embed")]), \
             mock.patch.object(small_model.small_models, "instances",
                               {"embed": inst}):
            out = asyncio.run(idx._embed_texts(["a"] * 5))
            self.assertEqual(len(out), 5)


class WorkspaceIndexTests(_TempDb):
    """index_workspace: walk filters (size, suffix, skip dirs), stale deletion
    and the embed->store path."""

    def _mk_ws(self):
        import core.memory as memory_pkg
        import tempfile
        ws = Path(tempfile.mkdtemp())
        (ws / "keep.md").write_text("hello world " * 50, encoding="utf-8")
        (ws / "image.png").write_text("not text", encoding="utf-8")         # not in TEXT_EXTS
        (ws / "node_modules").mkdir()
        (ws / "node_modules" / "x.js").write_text("skip me", encoding="utf-8")
        big = ws / "big.txt"
        big.write_text("x" * (MAX_FILE_BYTES + 1000), encoding="utf-8")
        return ws, memory_pkg

    def test_workspace_walk_filters_and_embeds(self):
        import asyncio
        from core.memory import indexing as idx
        ws, memory_pkg = self._mk_ws()
        sent = []

        async def fake_embed(texts):
            sent.extend(texts)
            return [[float(i)] for i in range(len(texts))]
        with mock.patch.object(memory_pkg, "_embed_texts", fake_embed), \
             mock.patch.object(memory_pkg, "_store_vecs", lambda rows, vecs: None):
            out = asyncio.run(idx.index_workspace(ws, force=True))
        self.assertEqual(out["files"], 1)          # only keep.md passes the filters
        self.assertGreater(out["chunks"], 0)
        self.assertTrue(any("hello world" in s for s in sent))

    def test_workspace_already_indexed_skips(self):
        import asyncio
        from core.memory import indexing as idx
        ws, memory_pkg = self._mk_ws()
        calls = {"n": 0}

        async def fake_embed(texts):
            calls["n"] += 1
            return [[float(i)] for i in range(len(texts))]
        # first pass stores real rows (no _store_vecs patch) so the second
        # pass hits the _already_indexed mtime check
        with mock.patch.object(memory_pkg, "_embed_texts", fake_embed):
            asyncio.run(idx.index_workspace(ws, force=True))
            n_after_first = calls["n"]
        # no force: the same mtime rows are already indexed -> no new embed
        with mock.patch.object(memory_pkg, "_embed_texts", fake_embed):
            asyncio.run(idx.index_workspace(ws))
        self.assertEqual(n_after_first, 1)
        self.assertEqual(calls["n"], 1)

    def test_missing_workspace_and_file_replaced_cleanly(self):
        import asyncio
        from core.memory import indexing as idx
        self.assertEqual(asyncio.run(idx.index_workspace(None)), {"files": 0, "chunks": 0})
        self.assertEqual(asyncio.run(idx.index_workspace(Path("/no/such/dir/qq"))),
                         {"files": 0, "chunks": 0})


class SessionIndexTests(_TempDb):
    def test_sessions_skipped_when_db_unavailable(self):
        import asyncio
        from core.memory import indexing as idx
        with mock.patch("core.db.db_session_docs", side_effect=RuntimeError("no db")):
            self.assertEqual(asyncio.run(idx.index_sessions()), 0)

    def test_sessions_embed_and_store(self):
        import asyncio
        import core.memory as memory_pkg
        from core.memory import indexing as idx
        docs = [("session/1", "hello session world " * 30, 1.0)]

        async def fake_embed(texts):
            return [[float(i)] for i in range(len(texts))]

        with mock.patch("core.db.db_session_docs", return_value=docs), \
             mock.patch.object(memory_pkg, "_embed_texts", fake_embed), \
             mock.patch.object(memory_pkg, "_store_vecs", lambda rows, vecs: None):
            n = asyncio.run(idx.index_sessions())
        self.assertGreater(n, 0)

class EnsureIndexedTests(_TempDb):
    def test_ensure_runs_once_within_window(self):
        import asyncio
        from core.memory import indexing as idx
        calls = {"n": 0}

        async def fake_workspace(*a, **k):
            calls["n"] += 1
            return {"files": 0, "chunks": 0}

        orig = idx._last_index
        idx._last_index = 0.0
        try:
            with mock.patch.object(idx, "index_workspace", fake_workspace), \
                 mock.patch.object(idx, "index_sessions", mock.AsyncMock(return_value=0)):
                asyncio.run(idx.ensure_indexed(max_age_s=0))     # runs
                asyncio.run(idx.ensure_indexed(max_age_s=3600))  # within 1h of _last_index: guarded
        finally:
            idx._last_index = orig
        self.assertEqual(calls["n"], 1)     # the _last_index guard dedupes


if __name__ == "__main__":
    unittest.main()
