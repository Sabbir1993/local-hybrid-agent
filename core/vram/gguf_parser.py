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
        # stat_ok False is load-bearing. This dict carries file_bytes 0, which used to make the
        # footprint estimate ~0 and the verdict "fit" - so a model file the server could not
        # read was treated as the one thing it was certain would not fail. Callers must treat
        # stat_ok False as "unknown, do not launch".
        return {"arch": "llama", "n_layer": 32, "n_head": 32, "n_head_kv": 8,
                "n_embd": None, "head_dim": 128, "ctx_train": 0, "n_expert": 0,
                "n_vocab": None, "file_bytes": 0, "complete": False, "stat_ok": False,
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
    # Sliding-window (hybrid attention) and the routed expert count. Both are read
    # OPPORTUNISTICALLY: they are in the same {arch}.attention.* / {arch}.feed_forward.* groups
    # the loop above already scans, so they are normally captured before the early exit. They
    # are deliberately NOT added to `needed`, because a model without them would then never
    # satisfy it and the header scan would run to completion - including the ~150k-entry
    # tokenizer string array below, one Python iteration per entry.
    #
    # Failing to read them is the SAFE direction: None means "charge the full context", which
    # over-estimates and can only produce a false "tight"/"nofit", never a false "fit".
    sliding_window = scalar(f"{arch}.attention.sliding_window")
    if sliding_window is not None:
        try:
            sliding_window = int(sliding_window)
        except (TypeError, ValueError):
            sliding_window = None
        if sliding_window is not None and sliding_window <= 0:
            sliding_window = None  # 0/-1 mean "no window", i.e. full attention
    n_expert_used = scalar(f"{arch}.expert_used_count")
    # Hybrid attention/linear models (Qwen3-Next family) keep a KV cache only in every Nth
    # layer; the rest hold a small fixed recurrent state. The key sits after the early exit
    # above, so it is read separately. Failing to read it is the safe direction: every layer
    # is then charged KV, which can only over-estimate.
    n_kv_layers = int(n_layer)
    interval = read_gguf_scalars(first, [f"{arch}.full_attention_interval"]).get(
        f"{arch}.full_attention_interval") if parse_err is None else None
    if isinstance(interval, int) and interval > 1:
        n_kv_layers = max(1, int(n_layer) // interval)

    info = {"arch": arch,
            "n_layer": int(n_layer), "n_head": int(n_head) if n_head else None,
            "n_head_kv": int(n_kv), "n_embd": int(n_embd) if n_embd else None,
            "head_dim": int(head_dim), "ctx_train": int(ctx_train or 0),
            "sliding_window": sliding_window,
            "n_expert": int(scalar(f"{arch}.expert_count", default=0) or 0),
            "n_expert_used": int(n_expert_used or 0),
            "n_kv_layers": n_kv_layers,
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


_EXPERT_NAME = re.compile(r"^blk\.(\d+)\..*_exps\.")
_expert_cache: dict = {}        # path -> ((mtime_ns, size), {layer: bytes})


def _skip_kv_value(cur, vtype: int) -> None:
    if vtype == 8:
        cur.skip(struct.unpack("<Q", cur.read(8))[0])
    elif vtype == 9:
        et = struct.unpack("<I", cur.read(4))[0]
        cnt = struct.unpack("<Q", cur.read(8))[0]
        if et == 8:
            for _ in range(cnt):
                cur.skip(struct.unpack("<Q", cur.read(8))[0])
        elif et in _GGUF_FMT:
            cur.skip(_GGUF_FMT[et][1] * cnt)
        else:
            raise ValueError(f"bad array elem type {et}")
    elif vtype in _GGUF_FMT:
        cur.skip(_GGUF_FMT[vtype][1])
    else:
        raise ValueError(f"unknown GGUF kv type {vtype}")


def expert_layer_bytes(model_path) -> "dict[int, int]":
    """Bytes of the routed-expert tensors (*_exps) of each transformer block, {} if unreadable.

    A mixture-of-experts file is ~90% expert weights, and `--n-cpu-moe N` moves exactly those
    for the first N blocks into RAM, so the VRAM estimate needs the real figure. Tensor sizes
    come from the offset gaps in the tensor table, so no quantisation block table is needed.
    Only the first shard is read; a split model returns {} and the caller falls back to a
    proportional estimate.
    """
    p = Path(model_path)
    try:
        st = p.stat()
    except OSError:
        return {}
    key = str(p)
    with _gguf_lock:
        hit = _expert_cache.get(key)
        if hit and hit[0] == (st.st_mtime_ns, st.st_size):
            return hit[1]
    out: dict = {}
    try:
        if _SHARD_RE.search(p.name):
            raise ValueError("split model")
        with open(p, "rb") as fh:
            cur = _Cursor(fh, chunk=1 * MIB)
            if cur.read(4) != b"GGUF":
                raise ValueError("not a GGUF file")
            if struct.unpack("<I", cur.read(4))[0] < 2:
                raise ValueError("unsupported GGUF version")
            n_tensors = struct.unpack("<Q", cur.read(8))[0]
            kv_count = struct.unpack("<Q", cur.read(8))[0]
            if kv_count > 100_000 or n_tensors > 1_000_000:
                raise ValueError("implausible GGUF counts")
            alignment = 32
            for _ in range(kv_count):
                klen = struct.unpack("<Q", cur.read(8))[0]
                name = cur.read(klen).decode("utf-8", "replace")
                vtype = struct.unpack("<I", cur.read(4))[0]
                if name == "general.alignment" and vtype in (4, 5):
                    alignment = max(1, struct.unpack(_GGUF_FMT[vtype][0], cur.read(4))[0])
                else:
                    _skip_kv_value(cur, vtype)
            tensors = []
            for _ in range(n_tensors):
                ln = struct.unpack("<Q", cur.read(8))[0]
                name = cur.read(ln).decode("utf-8", "replace")
                nd = struct.unpack("<I", cur.read(4))[0]
                cur.skip(8 * nd)
                cur.read(4)  # ggml type
                tensors.append((struct.unpack("<Q", cur.read(8))[0], name))
            # position of the end of the tensor table within the file
            table_end = cur.fh.tell() - (len(cur.buf) - cur.pos)
            data_start = -(-table_end // alignment) * alignment
        tensors.sort()
        for i, (off, name) in enumerate(tensors):
            m = _EXPERT_NAME.match(name)
            if not m:
                continue
            end = tensors[i + 1][0] if i + 1 < len(tensors) else st.st_size - data_start
            if end > off:
                layer = int(m.group(1))
                out[layer] = out.get(layer, 0) + (end - off)
    except Exception:
        out = {}
    with _gguf_lock:
        _expert_cache[key] = ((st.st_mtime_ns, st.st_size), out)
    return out
