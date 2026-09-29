import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from ..backend import DEVICE_PREFIX
from ..config import CONFIG_DEFAULTS
from .constants import GB, MIB, VRAM_WALL_FREE_B

_DEV_PREFIXES = "|".join(DEVICE_PREFIX.values())  # Vulkan, CUDA, ...
_LIST_DEV_RE = re.compile(
    rf"^\s*(?:{_DEV_PREFIXES})(\d+):\s*(.+?)\s*\(\s*([\d.]+)\s*(MiB|GiB)"
    r"(?:\s*,\s*([\d.]+)\s*(MiB|GiB)\s+free)?\s*\)\s*$"
)

_DEV_TTL_S = 10.0
_dev_cache: dict = {"ts": 0.0, "data": []}
_dev_lock = threading.Lock()


def _run_list_devices(bin_dir) -> list:
    base = Path(bin_dir or CONFIG_DEFAULTS["llama_bin_dir"])
    bench = None
    for name in ("llama-bench.exe", "llama-bench"):
        cand = base / name
        if cand.exists():
            bench = cand
            break
    if bench is None:
        return []
    try:
        out = subprocess.run([str(bench), "--list-devices"], capture_output=True,
                             text=True, errors="replace", timeout=25)
    except Exception as e:
        print(f"[vram] llama-bench --list-devices failed: {e}", file=sys.stderr)
        return []
    devs = []
    for line in (out.stdout or "").splitlines():
        m = _LIST_DEV_RE.match(line)
        if not m:
            continue
        idx = int(m.group(1))
        name = m.group(2).strip()
        total_b = int(float(m.group(3)) * (MIB if m.group(4) == "MiB" else GB))
        free_b = total_b
        if m.group(5):
            free_b = int(float(m.group(5)) * (MIB if m.group(6) == "MiB" else GB))
        free_b = max(0, min(free_b, total_b))
        devs.append({"index": idx, "name": name, "total_b": total_b,
                     "free_b": free_b, "used_b": total_b - free_b})
    return devs


def query_devices(llama_bin_dir=None, force: bool = False) -> list:
    """[{index, name, total_b, free_b, used_b}] per Vulkan device; 10s cache."""
    with _dev_lock:
        now = time.time()
        if (not force and _dev_cache["data"]
                and now - _dev_cache["ts"] < _DEV_TTL_S):
            return _dev_cache["data"]
        devs = _run_list_devices(llama_bin_dir)
        if devs:
            _dev_cache["ts"] = now
            _dev_cache["data"] = devs
        return _dev_cache["data"]


def wall_check(target_indices, devices: list) -> "int | None":
    """Called from state._wait_healthy while llama-server is loading.
    Returns the Vulkan index that hit the VRAM wall, or None."""
    if not devices:
        return None
    for d in devices:
        if d["index"] in target_indices and d["free_b"] <= VRAM_WALL_FREE_B:
            return d["index"]
    return None
