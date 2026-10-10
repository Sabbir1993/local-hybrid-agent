"""core/code_intel/remote.py - keep a symbol index of a workspace that lives on the user's device.

The server never holds the user's workspace (no-server-workspace policy), so the index is built from source
text fetched through the companion's `fs.read_many` op and kept only in this process's memory, per (user,
workspace root): symbols and call sites, never the source text itself. Every query re-syncs first, so an
edit the agent just made is visible; the sync is incremental (the device only sends files whose
[mtime, size] stamp differs from the one we hold), so a warm refresh is one stat-walk on the device.
"""

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass

from core import companion_bridge

from .ast_index import SUPPORTED_SUFFIXES, SourceIndex

OP = "fs.read_many"
MAX_ENTRIES = 12            # (user, root) indexes kept in memory
TTL_S = 30 * 60             # an index idle this long is dropped
MAX_ROUNDS = 16             # fetch batches per sync (each batch is capped by the device)
MAX_FILES = 5000
MAX_FILE_BYTES = 512 * 1024
BATCH_BYTES = 2 * 1024 * 1024


class CompanionTooOld(RuntimeError):
    """The connected SSL Local Agent does not have fs.read_many yet."""


@dataclass
class SyncResult:
    index: SourceIndex
    complete: bool          # every file the device holds was fetched (rounds and file cap not hit)
    truncated: bool         # the device found more than MAX_FILES matching files
    skipped_large: int


class _Entry:
    def __init__(self):
        self.index = SourceIndex()
        self.lock = asyncio.Lock()
        self.touched = time.monotonic()


_CACHE: "OrderedDict[tuple, _Entry]" = OrderedDict()


def clear() -> None:
    _CACHE.clear()


def _entry(uid: int, root: str) -> _Entry:
    now = time.monotonic()
    for key in [k for k, e in _CACHE.items() if now - e.touched > TTL_S]:
        del _CACHE[key]
    key = (uid, root)
    ent = _CACHE.get(key)
    if ent is None:
        ent = _CACHE[key] = _Entry()
        while len(_CACHE) > MAX_ENTRIES:
            _CACHE.popitem(last=False)
    ent.touched = now
    _CACHE.move_to_end(key)
    return ent


async def sync(uid: int, root: str) -> SyncResult:
    """Bring the index of `root` up to date with the device and return it."""
    ent = _entry(uid, root)
    async with ent.lock:
        idx = ent.index
        res: dict = {}
        complete = False
        for _ in range(MAX_ROUNDS):
            known = {rel: list(stamp) for rel, stamp in idx.stamps().items()}
            try:
                res = await companion_bridge.call(uid, OP, {
                    "root": root, "exts": sorted(SUPPORTED_SUFFIXES), "known": known,
                    "max_files": MAX_FILES, "max_file_bytes": MAX_FILE_BYTES, "budget_bytes": BATCH_BYTES})
            except Exception as e:
                if "unknown op" in str(e).lower():
                    raise CompanionTooOld(str(e)) from e
                raise
            for f in res.get("files") or []:
                idx.update(f["rel"], f["stamp"], (f.get("text") or "").encode("utf-8"))
            if not res.get("more"):
                complete = True
                break
        if complete:
            idx.retain(res.get("all") or [])
        return SyncResult(idx, complete and not res.get("truncated"), bool(res.get("truncated")),
                          int(res.get("skipped_large") or 0))
