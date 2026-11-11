"""GPU load, temperature, power and PCIe link on Linux, read from sysfs (no extra packages, no root for the usual files).

Per discrete GPU (a DRM card that exposes hwmon sensors; Intel iGPUs do not):
  temp_c    hwmon temp*_input (millidegrees), the "pkg" sensor when labelled, else the hottest one
  watts     hwmon power1_input / power1_average (microwatts), else the change of energy1_input (microjoules) over time
  limit_w   hwmon power1_max / power1_cap
  load_pct  amdgpu gpu_busy_percent, else the share of time the GT was not idle:
            xe   device/tile0/gt0/gtidle/idle_residency_ms
            i915 gt/gt0/rc6_residency_ms (or power/rc6_residency_ms)
  pcie      link speed/width of the PCIe root port above the card (the slot link). Arc cards sit behind a switch on the
            board, so the GPU function's own link files describe that internal link, not the slot.

Readings that need two samples (energy, idle residency) are None on the first call. Anything that cannot be read is None:
the UI shows "—", never an invented number.
"""
import re
import threading
import time
from pathlib import Path
from typing import Optional

DRM_ROOT = Path("/sys/class/drm")
_MIN_INTERVAL_S = 1.0           # calls closer together than this reuse the last result, so deltas stay meaningful
_GT_S_TO_GEN = {2.5: 1, 5.0: 2, 8.0: 3, 16.0: 4, 32.0: 5, 64.0: 6}

_lock = threading.Lock()
_prev: dict = {}                 # card -> {"t": monotonic s, "energy_uj": int, "idle_ms": int}
_last: dict = {"t": 0.0, "root": None, "data": []}


def _read(p: Path) -> Optional[str]:
    try:
        return p.read_text().strip()
    except (OSError, ValueError):
        return None


def _read_int(p: Path) -> Optional[int]:
    v = _read(p)
    try:
        return int(v) if v is not None else None
    except ValueError:
        return None


def _cards(root: Path) -> list:
    """card0, card1, ... (not the card0-DP-1 connector entries) that have hwmon sensors."""
    out = []
    try:
        entries = sorted(root.iterdir(), key=lambda p: (len(p.name), p.name))
    except OSError:
        return out
    for c in entries:
        if re.fullmatch(r"card\d+", c.name) and _hwmon_dir(c) is not None:
            out.append(c)
    return out


def _hwmon_dir(card: Path) -> Optional[Path]:
    try:
        hw = sorted((card / "device" / "hwmon").iterdir())
    except OSError:
        return None
    return hw[0] if hw else None


def _temp_c(hw: Path) -> Optional[float]:
    temps, pkg = [], None
    for f in sorted(hw.glob("temp*_input")):
        v = _read_int(f)
        if v is None:
            continue
        label = (_read(hw / f.name.replace("_input", "_label")) or "").lower()
        if label == "pkg":
            pkg = v
        temps.append(v)
    best = pkg if pkg is not None else (max(temps) if temps else None)
    return round(best / 1000.0, 1) if best is not None else None


def _power(hw: Path, card: str, now: float, prev: dict) -> tuple:
    """(watts, energy_uj_now) - watts from an instantaneous counter, else from the energy counter's change."""
    for name in ("power1_input", "power1_average"):
        v = _read_int(hw / name)
        if v is not None:
            return round(v / 1e6, 1), None
    e = _read_int(hw / "energy1_input")
    if e is None:
        return None, None
    p = prev.get(card)
    if p and p.get("energy_uj") is not None and now > p["t"] and e >= p["energy_uj"]:
        return round((e - p["energy_uj"]) / 1e6 / (now - p["t"]), 1), e
    return None, e


def _limit_w(hw: Path) -> Optional[float]:
    for name in ("power1_max", "power1_cap"):
        v = _read_int(hw / name)
        if v:
            return round(v / 1e6, 1)
    return None


def _idle_ms(card: Path) -> Optional[int]:
    for rel in ("device/tile0/gt0/gtidle/idle_residency_ms", "gt/gt0/rc6_residency_ms", "power/rc6_residency_ms"):
        v = _read_int(card / rel)
        if v is not None:
            return v
    return None


def _load(card: Path, name: str, now: float, prev: dict) -> tuple:
    """(load_pct, idle_ms_now)."""
    busy = _read_int(card / "device" / "gpu_busy_percent")          # amdgpu reports it directly
    if busy is not None:
        return float(max(0, min(100, busy))), None
    idle = _idle_ms(card)
    if idle is None:
        return None, None
    p = prev.get(name)
    if p and p.get("idle_ms") is not None and now > p["t"] and idle >= p["idle_ms"]:
        wall_ms = (now - p["t"]) * 1000.0
        return round(max(0.0, min(100.0, 100.0 * (1 - (idle - p["idle_ms"]) / wall_ms))), 1), idle
    return None, idle


def _gen(speed: Optional[str]) -> Optional[int]:
    m = re.match(r"([\d.]+)\s*GT/s", speed or "")
    return _GT_S_TO_GEN.get(float(m.group(1))) if m else None


def _pcie(card: Path) -> Optional[str]:
    """'Gen4 x16' for the slot link: the root port is the PCI device directly under the pciDDDD:BB host bridge."""
    try:
        dev = (card / "device").resolve()
    except OSError:
        return None
    parts = dev.parts
    host = next((i for i, p in enumerate(parts) if re.fullmatch(r"pci[0-9a-f]{4}:[0-9a-f]{2}", p)), None)
    candidates = []
    if host is not None and host + 1 < len(parts):
        candidates.append(Path(*parts[:host + 2]))                   # root port: the slot link
    candidates.append(dev)                                            # fallback: the GPU function itself
    for d in candidates:
        gen, width = _gen(_read(d / "current_link_speed")), _read(d / "current_link_width")
        if gen and width and width.isdigit() and int(width) > 0:
            return f"Gen{gen} x{width}"
    return None


def read_gpus(root: Path = DRM_ROOT) -> list:
    """[{card, temp_c, watts, limit_w, load_pct, pcie}] for each discrete GPU; [] when sysfs has none."""
    with _lock:
        now = time.monotonic()
        if _last["root"] == str(root) and now - _last["t"] < _MIN_INTERVAL_S:
            return [dict(r) for r in _last["data"]]
        rows = []
        for card in _cards(root):
            hw = _hwmon_dir(card)
            watts, energy = _power(hw, card.name, now, _prev)
            load, idle = _load(card, card.name, now, _prev)
            rows.append({"card": card.name, "temp_c": _temp_c(hw), "watts": watts, "limit_w": _limit_w(hw),
                         "load_pct": load, "pcie": _pcie(card)})
            _prev[card.name] = {"t": now, "energy_uj": energy, "idle_ms": idle}
        _last.update({"t": now, "root": str(root), "data": rows})
        return [dict(r) for r in rows]


def _reset_for_tests() -> None:
    with _lock:
        _prev.clear()
        _last.update({"t": 0.0, "root": None, "data": []})
