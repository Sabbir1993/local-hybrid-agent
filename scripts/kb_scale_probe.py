"""
scripts/kb_scale_probe.py - measure knowledge-base search latency and cache memory at N chunks.

Builds a synthetic corpus in memory (never touches memory.db / auth.db / any running server) and times
core.memory.search.search_knowledge_hybrid, so a change to the search path is judged on numbers.

    python scripts/kb_scale_probe.py                # 10k, 50k, 100k chunks
    python scripts/kb_scale_probe.py 200000 500000  # custom sizes

Reports build time, resident cache size, and p50/p95 query latency for an admin (all sources) and for a
restricted user (a few sources) -- the restricted case is where the ACL filter cost shows.
"""

import asyncio
import os
import random
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np                                    # noqa: E402

DIM = 768
SOURCES = 200                                         # documents the corpus is spread over
WORDS = ("payment gateway settlement merchant refund policy leave salary employee annual report bank "
         "customer account transfer limit fee charge branch loan interest deposit card terminal api "
         "security audit compliance risk board director revenue profit quarter growth sustainability "
         "emission energy governance training attendance holiday handbook").split()


def make_corpus(n: int, seed: int = 1):
    rnd = random.Random(seed)
    rng = np.random.default_rng(seed)
    mat = rng.standard_normal((n, DIM)).astype(np.float32)
    entries = []
    for i in range(n):
        text = " ".join(rnd.choice(WORDS) for _ in range(rnd.randint(90, 150)))
        entries.append(("knowledge", f"kb:{i % SOURCES}", text, mat[i]))
    return entries, rng.standard_normal(DIM).astype(np.float32)


async def run(n: int, repeats: int = 15) -> None:
    import core.memory as mem
    from core.memory import cache as mcache, search as msearch
    import core.auth_db as auth_db

    t0 = time.perf_counter()
    entries, qvec = make_corpus(n)
    t_gen = time.perf_counter() - t0
    t0 = time.perf_counter()
    built = mcache._build_cache(entries)
    t_build = time.perf_counter() - t0

    async def fake_embed(texts):
        return [qvec.tolist()]

    mem._cached_entries = lambda: built
    mem._embed_texts = fake_embed
    msearch.search_fts5 = lambda *a, **kw: []
    auth_db.list_knowledge_sources = lambda: [{"id": i, "title": f"Doc {i}", "status": "ready"} for i in range(SOURCES)]

    mb = built["mat"].nbytes / 1e6
    txt = sum(len(e[2]) for e in entries) * 2 / 1e6        # text + lowercase copy
    print(f"\n== {n:,} chunks  (corpus {t_gen:.1f}s, cache build {t_build:.2f}s, matrix {mb:.0f} MB, text ~{txt:.0f} MB)")
    for label, allowed in (("admin  (all 200 sources)", set(range(SOURCES))), ("user   (5 sources)", {3, 17, 42, 99, 150})):
        times = []
        for _ in range(repeats):
            t0 = time.perf_counter()
            await msearch.search_knowledge_hybrid("merchant settlement refund policy", k=6,
                                                  allowed_knowledge_source_ids=allowed, min_cos=0.0, rerank_min_cos=0.0)
            times.append((time.perf_counter() - t0) * 1000)
        times.sort()
        print(f"   {label}: p50 {statistics.median(times):7.0f} ms   p95 {times[int(len(times) * .95) - 1]:7.0f} ms")


if __name__ == "__main__":
    sizes = [int(a) for a in sys.argv[1:]] or [10_000, 50_000, 100_000]
    for s in sizes:
        asyncio.run(run(s))
