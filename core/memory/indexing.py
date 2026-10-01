import asyncio
import sys
import time
from pathlib import Path
from typing import Optional

from .constants import (
    CHUNK_CHARS,
    CHUNK_OVERLAP,
    EMBED_BATCH,
    EMBEDDER_RETRY_S,
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_KB_CHUNKS_PER_DOC,
    MAX_SESSIONS,
    SKIP_DIRS,
    TEXT_EXTS,
    _chunk_text,
)
from .store import _already_indexed, _db, _delete_stale, _store_vecs

_embedder_down_until = 0.0
_index_lock = asyncio.Lock()
_last_index = 0.0


async def _embed_texts(texts: list) -> Optional[list]:
    """Embed via the already-running nomic-embed llama-server (port 8093)."""
    global _embedder_down_until
    if not texts:
        return None
    if time.time() < _embedder_down_until:
        return None
    try:
        from .. import lanes
        from ..small_model import small_models
        route = lanes.targets("embed")
        inst = small_models.instances.get(route[0].lane) if route else None
        if inst is None or not inst.available:
            _embedder_down_until = time.time() + EMBEDDER_RETRY_S
            return None
        await inst.ensure_loaded()
        out = []
        for i in range(0, len(texts), EMBED_BATCH):
            batch = [t[:4000] for t in texts[i:i + EMBED_BATCH]]
            r = await inst.client.post("/v1/embeddings", json={"input": batch})
            r.raise_for_status()
            data = r.json().get("data") or []
            out.extend(item.get("embedding") or [] for item in data)
            inst.last_used = time.time()
        if len(out) != len(texts) or any(not v for v in out):
            return None
        return out
    except Exception as e:
        print(f"[memory] embedder unavailable ({e}) - lexical-only recall", file=sys.stderr)
        _embedder_down_until = time.time() + EMBEDDER_RETRY_S
        return None


async def index_workspace(ws_path: Optional[Path] = None, force: bool = False) -> dict:
    if ws_path is None:
        return {"files": 0, "chunks": 0}
    ws = Path(ws_path)
    if not ws.exists():
        return {"files": 0, "chunks": 0}
    files = []
    try:
        for f in ws.rglob("*"):
            if len(files) >= MAX_FILES:
                break
            if not f.is_file():
                continue
            try:
                rel = str(f.relative_to(ws)).replace("\\", "/")
            except ValueError:
                continue
            if any(part in SKIP_DIRS for part in Path(rel).parts):
                continue
            if f.suffix.lower() not in TEXT_EXTS:
                continue
            try:
                if f.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            files.append(f)
    except Exception as e:
        print(f"[memory] workspace walk failed: {e}", file=sys.stderr)
        return {"files": 0, "chunks": 0}

    new_rows = []
    seen = set()
    for f in files:
        try:
            rel = str(f.relative_to(ws)).replace("\\", "/")
        except ValueError:
            continue
        seen.add(rel)
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        if not force and _already_indexed("workspace", rel, mtime):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for idx, part in enumerate(_chunk_text(text)):
            new_rows.append(("workspace", rel, idx, part, mtime))

    _delete_stale("workspace", seen)
    if new_rows:
        embed_fn = getattr(sys.modules.get("core.memory"), "_embed_texts", _embed_texts)
        vecs = await embed_fn([r[3] for r in new_rows])
        store_fn = getattr(sys.modules.get("core.memory"), "_store_vecs", _store_vecs)
        store_fn(new_rows, vecs)
    return {"files": len(files), "chunks": len(new_rows)}


async def index_sessions(force: bool = False) -> int:
    try:
        from ..db import db_session_docs
        docs = db_session_docs(MAX_SESSIONS)
    except Exception as e:
        print(f"[memory] session docs unavailable: {e}", file=sys.stderr)
        return 0
    rows = []
    seen = set()
    for path, text, version in docs:
        seen.add(path)
        if not force and _already_indexed("session", path, version):
            continue
        for idx, part in enumerate(_chunk_text(text)[:4]):
            rows.append(("session", path, idx, part, version))
    _delete_stale("session", seen)
    if rows:
        embed_fn = getattr(sys.modules.get("core.memory"), "_embed_texts", _embed_texts)
        vecs = await embed_fn([r[3] for r in rows])
        store_fn = getattr(sys.modules.get("core.memory"), "_store_vecs", _store_vecs)
        store_fn(rows, vecs)
    return len(rows)


async def index_knowledge_source(source_id: int, text: str, version: Optional[float] = None) -> int:
    path = f"kb:{source_id}"
    version = version if version is not None else time.time()
    _db().execute("DELETE FROM chunks WHERE source='knowledge' AND path=?", (path,))
    _db().commit()
    rows = [("knowledge", path, idx, part, version)
            for idx, part in enumerate(_chunk_text(text, MAX_KB_CHUNKS_PER_DOC))]
    if rows:
        embed_fn = getattr(sys.modules.get("core.memory"), "_embed_texts", _embed_texts)
        vecs = await embed_fn([r[3] for r in rows])
        store_fn = getattr(sys.modules.get("core.memory"), "_store_vecs", _store_vecs)
        store_fn(rows, vecs)
    return len(rows)


async def ensure_indexed(max_age_s: float = 900) -> None:
    global _last_index
    if time.time() - _last_index < max_age_s:
        return
    async with _index_lock:
        if time.time() - _last_index < max_age_s:
            return
        await index_workspace()
        await index_sessions()
        _last_index = time.time()


async def memory_background_task():
    await asyncio.sleep(6)
    while True:
        try:
            await ensure_indexed(max_age_s=0)
        except Exception as e:
            print(f"[memory] background index error: {e}", file=sys.stderr)
        await asyncio.sleep(900)
