"""One-shot: embed the R1 eval corpus through the live :8093 embedder.

Reads the corpus from tests/eval_mock.py (single source of truth - the cache
can never drift from the slice). Writes tests/.eval_vec_cache.json keyed by
sha1(text). The slice's real mode reads ONLY this file: reruns are GPU-free.

Usage: boot the embedder lane (or standalone llama-server --embedding on
:8093), run `python scripts/embed_eval_corpus.py`, shut the embedder down.
"""
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "tests"))

from eval_mock import RETRIEVAL_DOCS, RETRIEVAL_QUESTIONS  # noqa: E402
from core.memory.constants import (  # noqa: E402
    CHUNK_CHARS,
    CHUNK_OVERLAP,
    CHUNK_SNAP,
    _chunk_text,
)

CACHE = BASE / "tests" / ".eval_vec_cache.json"
ENDPOINT = "http://127.0.0.1:8093/v1/embeddings"


def main() -> None:
    # Indexing embeds CHUNKS, not doc bodies: cache exactly what the slice will
    # embed (unique chunk texts + all questions). Chunking is deterministic, so
    # the cache pins the chunk params - a chunk-size change (R5) must re-run
    # this script, and the slice refuses a stale cache instead of measuring
    # vectors cut with different boundaries.
    seen = set()
    texts = []
    for _, body in RETRIEVAL_DOCS:
        for part in _chunk_text(body):
            if part not in seen:
                seen.add(part)
                texts.append(part)
    texts += [q for q, _, _ in RETRIEVAL_QUESTIONS]
    print(f"embedding {len(texts)} texts via {ENDPOINT} ...")
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps({"input": texts}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        data = json.load(r)["data"]
    vecs = [d["embedding"] for d in sorted(data, key=lambda d: d["index"])]
    assert len(vecs) == len(texts) and all(vecs), "short/empty embedding batch"
    dim = len(vecs[0])
    assert all(len(v) == dim for v in vecs), "ragged embedding dims"
    cache = {
        "model": "nomic-embed-text-v1.5-Q8_0.gguf",
        "dim": dim,
        "chunk_chars": CHUNK_CHARS,
        "chunk_overlap": CHUNK_OVERLAP,
        "chunk_snap": CHUNK_SNAP,
        "n_texts": len(texts),
        "vecs": {hashlib.sha1(t.encode()).hexdigest(): v for t, v in zip(texts, vecs)},
    }
    CACHE.write_text(json.dumps(cache))
    print(f"DIM {dim} N {len(texts)} -> {CACHE} ({CACHE.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
