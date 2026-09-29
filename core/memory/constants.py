import json
import re
from typing import Optional

try:
    import numpy as _np
except Exception:
    _np = None

CHUNK_CHARS = 900
CHUNK_OVERLAP = 150
MAX_FILE_BYTES = 512 * 1024
MAX_FILES = 400
MAX_CHUNKS_PER_DOC = 40
MAX_SESSIONS = 60
EMBED_BATCH = 16
EMBEDDER_RETRY_S = 600  # retry the embedder 10 min after a failure
TEXT_EXTS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".css", ".json", ".md",
    ".txt", ".csv", ".yml", ".yaml", ".xml", ".sh", ".bat", ".ps1", ".php",
    ".sql", ".ini", ".toml", ".log",
}
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv"}


def _chunk_text(text: str) -> list:
    text = text.strip()
    if not text:
        return []
    if len(text) <= CHUNK_CHARS:
        return [text]
    chunks = []
    step = CHUNK_CHARS - CHUNK_OVERLAP
    i = 0
    while i < len(text) and len(chunks) < MAX_CHUNKS_PER_DOC:
        part = text[i:i + CHUNK_CHARS]
        if part.strip():
            chunks.append(part)
        i += step
    return chunks


def _knowledge_id_from_path(path: str) -> Optional[int]:
    if path.startswith("kb:"):
        try:
            return int(path.split(":", 1)[1])
        except ValueError:
            return None
    return None


def _session_id_from_path(path: str) -> Optional[int]:
    if path.startswith("session:"):
        try:
            return int(path.split(":", 1)[1])
        except ValueError:
            return None
    return None


def _decode_vec(vec):
    if vec is None:
        return None
    try:
        raw = bytes(vec)
        if _np is not None:
            return _np.frombuffer(raw, dtype=_np.float32)
        return json.loads(raw.decode())
    except Exception:
        return None


def _norm(vals: list) -> list:
    if not vals:
        return []
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        return [0.0] * len(vals)
    return [(v - lo) / (hi - lo) for v in vals]
