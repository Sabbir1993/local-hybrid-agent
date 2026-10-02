import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from ..backend import DEVICE_PREFIX
from ..config import CONFIG_DEFAULTS
from .constants import GB, MIB

_DEV_PREFIXES = "|".join(DEVICE_PREFIX.values())  # Vulkan, CUDA, ...
_LIST_DEV_RE = re.compile(
    rf"^\s*(?:{_DEV_PREFIXES})(\d+):\s*(.+?)\s*\(\s*([\d.]+)\s*(MiB|GiB)"
    r"(?:\s*,\s*([\d.]+)\s*(MiB|GiB)\s+free)?\s*\)\s*$"
)

_DEV_TTL_S = 10.0
_dev_cache: dict = {"key": None, "ts": 0.0, "data": []}
_dev_lock = threading.Lock()


class DeviceQueryError(RuntimeError):
    """The device list could not be DETERMINED.

    Distinct from "this machine reports zero devices". Every way of failing to ask
    (llama-bench missing, the subprocess crashing or timing out, the driver in a bad state,
    or a llama.cpp release changing the --list-devices line format) used to collapse into the
    same empty list as a machine with no GPU. That made the whole VRAM safety layer vanish
    silently: preflight saw no devices, returned status="unknown", and state._wait_healthy's
    wall_check returned None immediately, so the load was attempted and then trusted.

    Callers that need to tell "no GPU" from "cannot tell" use query_devices(strict=True).
    Soft callers keep the historical [] contract.
    """


def _run_list_devices(bin_dir) -> list:
    base = Path(bin_dir or CONFIG_DEFAULTS["llama_bin_dir"])
    bench = None
    for name in ("llama-bench.exe", "llama-bench"):
        cand = base / name
        if cand.exists():
            bench = cand
            break
    if bench is None:
        raise DeviceQueryError(
            f"llama-bench not found in {base} (looked for llama-bench.exe, llama-bench)")
    try:
        out = subprocess.run([str(bench), "--list-devices"], capture_output=True,
                             text=True, errors="replace", timeout=25)
    except Exception as e:
        raise DeviceQueryError(f"llama-bench --list-devices failed: {e}") from e
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
    if not devs:
        # The binary ran and we still got nothing parseable. Either the driver reports no
        # device (not a configuration this app supports) or the output format drifted - both
        # mean the same thing to a caller that needs to decide whether to trust a verdict.
        # Say so rather than returning an empty list that reads as "zero devices".
        head = (out.stdout or "").strip().splitlines()[:3]
        detail = " | ".join(h.strip() for h in head) or "(no output)"
        raise DeviceQueryError(
            "llama-bench --list-devices produced no parseable device lines; "
            f"first lines were: {detail}")
    return devs


def query_devices(llama_bin_dir=None, force: bool = False, strict: bool = False) -> list:
    """[{index, name, total_b, free_b, used_b}] per Vulkan device; 10s cache.

    strict=True raises DeviceQueryError instead of returning [] when the list cannot be
    determined. The cache is keyed by the resolved bin dir: it used to be a single global, so
    switching LLAMA_RUNTIME (vulkan -> cuda) served the other backend's devices for up to 10s,
    and after a failure it kept returning the previous list while leaving the timestamp stale -
    so a preflight could approve against hour-old free_b.
    """
    base = Path(llama_bin_dir or CONFIG_DEFAULTS["llama_bin_dir"])
    key = str(base)
    with _dev_lock:
        now = time.time()
        fresh = _dev_cache["key"] == key and _dev_cache["data"] \
            and now - _dev_cache["ts"] < _DEV_TTL_S
        if not force and fresh:
            return _dev_cache["data"]
        try:
            devs = _run_list_devices(base)
        except DeviceQueryError:
            # Drop the previous backend's/stale list rather than serving it against a new
            # bin dir or after a failed query.
            _dev_cache.update({"key": None, "ts": 0.0, "data": []})
            if strict:
                raise
            print("[vram] device list unavailable - VRAM checks will be skipped",
                  file=sys.stderr)
            return []
        _dev_cache.update({"key": key, "ts": now, "data": devs})
        return devs


def wall_check(target_indices, devices: list) -> "int | None":
    """Called from state._wait_healthy while llama-server is loading.
    Returns the Vulkan index that hit the VRAM wall, or None.

    The threshold was a module constant with no way to change it, even though it is the knob
    that decides whether a load is aborted: a card with a lot of shared/unaccounted usage may
    trip it on a model that would actually have fit. Now runtime.vram_wall_free_mb."""
    from ..supervision import vram_wall_free_b
    threshold = vram_wall_free_b()
    if not devices:
        return None
    for d in devices:
        if d["index"] in target_indices and d["free_b"] <= threshold:
            return d["index"]
    return None
