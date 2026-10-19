"""
scripts/sync_knowledge_base.py - CLI tool to inspect, sync, and test the Tri-Modal Knowledge Base.

Usage:
  python scripts/sync_knowledge_base.py --status
  python scripts/sync_knowledge_base.py --sync
  python scripts/sync_knowledge_base.py --query "who is the manager of engineering"
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.memory.store import _db
from core.memory.okf import backfill_okf_from_chunks, search_okf
from core.knowledge_router import determine_retrieval_scope, fetch_company_knowledge
from core.auth_db import list_knowledge_sources


def print_status():
    db = _db()
    n_chunks = db.execute("SELECT COUNT(*) FROM chunks WHERE source='knowledge'").fetchone()[0]
    n_vecs = db.execute("SELECT COUNT(*) FROM chunks WHERE source='knowledge' AND vec IS NOT NULL").fetchone()[0]
    n_entities = db.execute("SELECT COUNT(*) FROM kb_entities").fetchone()[0]
    n_relations = db.execute("SELECT COUNT(*) FROM kb_relations").fetchone()[0]
    n_links = db.execute("SELECT COUNT(*) FROM kb_links").fetchone()[0]

    sources = list_knowledge_sources()
    ready_sources = [s for s in sources if dict(s).get("status") == "ready"]

    print("\n=======================================================")
    print("      TRI-MODAL KNOWLEDGE BASE STATUS OVERVIEW")
    print("=======================================================")
    print(f"  Total Registered Sources : {len(sources)} ({len(ready_sources)} ready)")
    print(f"  Total Knowledge Chunks   : {n_chunks:,}")
    print(f"  Embedded Vector Chunks   : {n_vecs:,} ({(n_vecs / max(1, n_chunks))*100:.1f}%)")
    print(f"  OKF Entities Extracted   : {n_entities:,}")
    print(f"  OKF Relational Triples   : {n_relations:,}")
    print(f"  Intra-Document Links     : {n_links:,}")
    print("=======================================================\n")


def run_sync():
    print("[*] Running OKF graph extraction & synchronization...")
    backfilled = backfill_okf_from_chunks()
    print(f"[+] Successfully synchronized {backfilled} sources.")
    print_status()


async def run_test_query(query: str):
    print(f"\n[*] Testing Query: '{query}'")
    scope = determine_retrieval_scope(query)
    print(f"[*] Scope Decision: {scope['scope'].upper()} (Weights: {scope['weights']})")

    sources = list_knowledge_sources()
    source_ids = {dict(s)["id"] for s in sources if dict(s).get("status") == "ready"}
    if not source_ids:
        db = _db()
        # Fallback to distinct source IDs from chunks in memory.db
        source_ids = {int(r[0].split(":")[1]) for r in db.execute("SELECT DISTINCT path FROM chunks WHERE source='knowledge' AND path LIKE 'kb:%'").fetchall() if ":" in r[0]}

    print(f"[*] Searching across {len(source_ids)} active knowledge sources...")
    okf_facts = search_okf(query, allowed_source_ids=source_ids, k=3)
    if okf_facts:
        print("\n--- OKF Structured Facts Matched ---")
        for f in okf_facts:
            print(f"  • {f['fact']} (Score: {f['score']:.2f}, Source #{f['source_id']})")

    try:
        hits, prompt_block = await asyncio.wait_for(
            fetch_company_knowledge(query, allowed_source_ids=source_ids, k=4),
            timeout=4.0
        )
    except asyncio.TimeoutError:
        print("\n[!] Embedder daemon not running; executing fast sparse BM25 + OKF fallback...")
        from core.memory.search import search_fts5
        from core.memory.reranker import rerank_and_filter
        fts_hits = search_fts5(query, k=8, sources={"knowledge"}, allowed_knowledge_source_ids=source_ids)
        for h in fts_hits:
            h["score"] = min(1.0, 0.4 + (0.1 * min(5, h.get("bm25", 1))))
        hits = rerank_and_filter(query, fts_hits, top_k=4, okf_facts=okf_facts)

    print(f"\n--- Top Reranked Snippets ({len(hits)} found) ---")
    for i, h in enumerate(hits):
        print(f"  [{i+1}] Source: {h.get('title', 'Doc')} | Conf: {h.get('final_score', h.get('score', 0)):.2f}")
        preview = h.get('text', '').strip()[:140].replace('\n', ' ')
        print(f"      {preview}...\n")


def main():
    parser = argparse.ArgumentParser(description="Tri-Modal Knowledge Base CLI Tool")
    parser.add_argument("--status", action="store_true", help="Print knowledge base statistics")
    parser.add_argument("--sync", action="store_true", help="Synchronize and extract OKF graphs for all sources")
    parser.add_argument("--query", type=str, help="Run a test query through the Tri-Modal pipeline")

    args = parser.parse_args()

    if args.status or (not args.sync and not args.query):
        print_status()
    elif args.sync:
        run_sync()
    elif args.query:
        asyncio.run(run_test_query(args.query))
    sys.exit(0)


if __name__ == "__main__":
    main()
