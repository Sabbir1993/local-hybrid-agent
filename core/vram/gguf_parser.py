import re
import struct
import threading
from pathlib import Path

from .constants import MIB

_GGUF_FMT = {0: ("<B", 1), 1: ("<b", 1), 2: ("<H", 2), 3: ("<h", 2),
             4: ("<I", 4), 5: ("<i", 4), 6: ("<f", 4), 7: ("<B", 1),
             10: ("<Q", 8), 11: ("<q", 8), 12: ("<d", 8)}

_SHARD_RE = re.compile(r"-(\d{5})-of-(\d{5})\.gguf$", re.I)

_gguf_cache: dict = {}          # path -> ((mtime_ns, size), info)
_gguf_lock = threading.Lock()


class _Cursor:
    """Incremental reader over the first few MB of a (possibly huge) GGUF."""

    def __init__(self, fh, chunk=4 * MIB):
        self.fh = fh
        self.buf = b""
        self.pos = 0
        self.chunk = chunk

    def _need(self, n: int) -> bool:
        if len(self.buf) - self.pos >= n:
            return True
        while len(self.buf) - self.pos < n:
            data = self.fh.read(max(self.chunk, n - (len(self.buf) - self.pos)))
            if not data:
                return False
            self.buf = self.buf[self.pos:] + data
            self.pos = 0
        return True

    def read(self, n: int) -> bytes:
        if not self._need(n):
            raise EOFError("unexpected EOF inside GGUF header")
        out = self.buf[self.pos:self.pos + n]
        self.pos += n
        return out

    def skip(self, n: int) -> None:
        while n > 0:
            step = min(n, self.chunk)
            if not self._need(step):
                raise EOFError("unexpected EOF inside GGUF header")
            self.pos += step
            n -= step


def _gguf_files(model_path: Path):
    """(total_bytes_of_all_shards, path_of_first_shard) - handles split GGUFs."""
    m = _SHARD_RE.search(model_path.name)
    if m:
        prefix = model_path.name[:m.start()]
        parts = sorted(model_path.parent.glob(
            f"{prefix}-?????-of-{m.group(2)}.gguf"))
        if parts:
            return sum(p.stat().st_size for p in parts), parts[0]
    return model_path.stat().st_size, model_path


def parse_gguf_info(model_path) -> "dict | None":
    """Parse GGUF metadata needed for the VRAM estimate."""
    p = Path(model_path)
    if not p.exists():
        return None
    with _gguf_lock:
        try:
            st = p.stat()
        except OSError:
            return None
        cached = _gguf_cache.get(str(p))
        if cached and cached[0] == (st.st_mtime_ns, st.st_size):
            return cached[1]

    parse_err = None
    raw: dict = {}
    try:
        total_b, first = _gguf_files(p)
    except OSError as e:
        return {"arch": "llama", "n_layer": 32, "n_head": 32, "n_head_kv": 8,
                "n_embd": None, "head_dim": 128, "ctx_train": 0, "n_expert": 0,
                "n_vocab": None, "file_bytes": 0, "complete": False,
                "model_path": str(p), "error": str(e)}
    try:
        with open(first, "rb") as fh:
            cur = _Cursor(fh)
            if cur.read(4) != b"GGUF":
                raise ValueError("not a GGUF file")
            version = struct.unpack("<I", cur.read(4))[0]
            if version < 2:
                raise ValueError(f"unsupported GGUF version {version}")
            cur.read(8)  # tensor count
            kv_count = struct.unpack("<Q", cur.read(8))[0]
            if kv_count > 100_000:
                raise ValueError("implausible GGUF kv_count")
            for _ in range(kv_count):
                klen = struct.unpack("<Q", cur.read(8))[0]
                key = cur.read(klen).decode("utf-8", "replace")
                vtype = struct.unpack("<I", cur.read(4))[0]
                if vtype == 8:  # string
                    ln = struct.unpack("<Q", cur.read(8))[0]
                    if key == "general.architecture":
                        raw[key] = cur.read(ln).decode("utf-8", "replace")
                    else:
                        cur.skip(ln)
                elif vtype == 9:  # array
                    et = struct.unpack("<I", cur.read(4))[0]
                    cnt = struct.unpack("<Q", cur.read(8))[0]
                    if key == "tokenizer.ggml.tokens":
                        raw["n_vocab"] = cnt
                    if et == 8:
                        for _ in range(cnt):
                            ln = struct.unpack("<Q", cur.read(8))[0]
                            cur.skip(ln)
                    elif et in _GGUF_FMT:
                        _, sz = _GGUF_FMT[et]
                        if 0 < cnt <= 1024:
                            raw[key] = [struct.unpack(_GGUF_FMT[et][0],
                                                      cur.read(sz))[0]
                                        for _ in range(cnt)]
                        else:
                            cur.skip(sz * cnt)
                    else:
                        raise ValueError(f"bad array elem type {et}")
                elif vtype in _GGUF_FMT:
                    fmt, sz = _GGUF_FMT[vtype]
                    raw[key] = struct.unpack(fmt, cur.read(sz))[0]
                else:
                    raise ValueError(f"unknown GGUF kv type {vtype}")
                arch = raw.get("general.architecture")
                if arch:
                    needed = (f"{arch}.block_count",
                              f"{arch}.attention.head_count",
                              f"{arch}.attention.head_count_kv",
                              f"{arch}.embedding_length",
                              f"{arch}.attention.key_length",
                              f"{arch}.context_length",
                              f"{arch}.expert_count")
                    if all(k in raw for k in needed):
                        break
    except Exception as e:
        parse_err = str(e)

    arch = raw.get("general.architecture") or "llama"

    def scalar(*keys, default=None):
        for k in keys:
            if k in raw:
                v = raw[k]
                if isinstance(v, list):
                    return max(v) if v else default
                return v
        return default

    n_layer = scalar(f"{arch}.block_count")
    n_head = scalar(f"{arch}.attention.head_count")
    n_kv = scalar(f"{arch}.attention.head_count_kv")
    n_embd = scalar(f"{arch}.embedding_length")
    head_dim = scalar(f"{arch}.attention.key_length",
                      f"{arch}.attention.value_length")
    complete = True
    if head_dim is None and n_embd and n_head:
        head_dim = int(n_embd) // int(n_head)
    if head_dim is None:
        head_dim, complete = 128, False
    if n_kv is None:
        n_kv = n_head
        if n_kv is None:
            n_kv, complete = 8, False
    if n_layer is None:
        n_layer, complete = 32, False
    ctx_train = scalar(f"{arch}.context_length", default=0)

    info = {"arch": arch,
            "n_layer": int(n_layer), "n_head": int(n_head) if n_head else None,
            "n_head_kv": int(n_kv), "n_embd": int(n_embd) if n_embd else None,
            "head_dim": int(head_dim), "ctx_train": int(ctx_train or 0),
            "n_expert": int(scalar(f"{arch}.expert_count", default=0) or 0),
            "n_vocab": raw.get("n_vocab"), "file_bytes": total_b,
            "complete": complete, "model_path": str(p), "error": parse_err}
    with _gguf_lock:
        _gguf_cache[str(p)] = ((st.st_mtime_ns, st.st_size), info)
    return info


def read_gguf_scalars(model_path, keys) -> dict:
    """Read selected scalar/string metadata keys from a GGUF header."""
    want = set(keys)
    out: dict = {}
    try:
        with open(model_path, "rb") as fh:
            cur = _Cursor(fh, chunk=1 * MIB)
            if cur.read(4) != b"GGUF":
                return {}
            if struct.unpack("<I", cur.read(4))[0] < 2:
                return {}
            cur.read(8)
            kv_count = struct.unpack("<Q", cur.read(8))[0]
            if kv_count > 100_000:
                return {}
            for _ in range(kv_count):
                klen = struct.unpack("<Q", cur.read(8))[0]
                key = cur.read(klen).decode("utf-8", "replace")
                vtype = struct.unpack("<I", cur.read(4))[0]
                if vtype == 8:
                    ln = struct.unpack("<Q", cur.read(8))[0]
                    if key in want:
                        out[key] = cur.read(ln).decode("utf-8", "replace")
                    else:
                        cur.skip(ln)
                elif vtype == 9:
                    et = struct.unpack("<I", cur.read(4))[0]
                    cnt = struct.unpack("<Q", cur.read(8))[0]
                    if et == 8:
                        for _ in range(cnt):
                            cur.skip(struct.unpack("<Q", cur.read(8))[0])
                    elif et in _GGUF_FMT:
                        cur.skip(_GGUF_FMT[et][1] * cnt)
                    else:
                        return out
                elif vtype in _GGUF_FMT:
                    fmt, sz = _GGUF_FMT[vtype]
                    v = struct.unpack(fmt, cur.read(sz))[0]
                    if key in want:
                        out[key] = v
                else:
                    return out
                if want.issubset(out):
                    break
    except Exception:
        pass
    return out
