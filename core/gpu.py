import asyncio
import json
import subprocess
import sys
import time

from .config import GPU_QUERY_INTERVAL_S, IGNORED_IGPU_LUIDS, CONFIG_FILE, CONFIG_DEFAULTS
from . import vram

PS_GPU_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$adapters = @()
$mem = Get-Counter '\GPU Adapter Memory(*)\Dedicated Usage'
if ($mem) {
  foreach ($s in $mem.CounterSamples) {
    $p = $s.InstanceName.Split('_')
    if ($p.Count -ge 3 -and $p[1] -like '0x*') {
      $adapters += [pscustomobject]@{ luid = $p[2].ToLower(); gb = [math]::Round($s.CookedValue/1GB, 2) }
    }
  }
}
$agg = @{}
$eng = Get-Counter '\GPU Engine(*)\Utilization Percentage'
if ($eng) {
  foreach ($s in $eng.CounterSamples) {
    if ($s.InstanceName -like 'pid_*engtype_compute*') {
      $p = $s.InstanceName.Split('_')
      if ($p.Count -ge 5 -and $p[4] -like '0x*') {
        $key = "$($p[4].ToLower())|$($p[1])"
        if (-not $agg.ContainsKey($key)) { $agg[$key] = 0.0 }
        $agg[$key] += $s.CookedValue
      }
    }
  }
}
$compute = @()
foreach ($k in $agg.Keys) {
  $parts = $k.Split('|')
  $compute += [pscustomobject]@{ luid = $parts[0]; pid = $parts[1]; pct = [math]::Round($agg[$k], 1) }
}
[pscustomobject]@{ adapters = $adapters; compute = $compute } | ConvertTo-Json -Compress -Depth 4
"""

_gpu_cache = {"ts": 0.0, "data": {"adapters": [], "compute": []}}
_gpu_query_lock = asyncio.Lock()


def _query_gpu_sync() -> dict:
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", PS_GPU_SCRIPT],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(proc.stdout.strip())
        if isinstance(data, dict):
            raw_adapters = data.get("adapters", [])
            # Deduplicate by luid taking max gb, and filter out iGPU entries
            dedup = {}
            for a in raw_adapters:
                luid = str(a.get("luid", "")).lower()
                if not luid or luid in IGNORED_IGPU_LUIDS:
                    continue
                gb = float(a.get("gb", 0))
                # iGPUs typically show tiny dedicated RAM (<1.0GB)
                if gb < 1.0:
                    continue
                if luid not in dedup or gb > dedup[luid]["gb"]:
                    dedup[luid] = {"luid": luid, "gb": gb}
            
            data["adapters"] = list(dedup.values())
            return data
    except Exception as e:
        print(f"[server_manager] gpu query failed: {e}", file=sys.stderr)
    return {"adapters": [], "compute": []}


# GPU count / total VRAM / backend don't change while the manager runs, but
# computing them spawns `llama-bench --list-devices` -- far too slow to do on the
# event loop for every /control/status poll. Cache them for _HW_TTL_S.
_HW_TTL_S = 300.0
_hw_cache = {"ts": 0.0, "summary": None, "totals": None}


def _hw_fresh() -> bool:
    return _hw_cache["summary"] is not None and time.time() - _hw_cache["ts"] < _HW_TTL_S


def _refresh_hw_sync() -> None:
    _hw_cache["summary"] = _compute_hardware_engine_summary()
    _hw_cache["totals"] = _compute_discrete_vram_totals_gb()
    _hw_cache["ts"] = time.time()


async def refresh_hw_async() -> None:
    """Refresh the hardware cache off the event loop (no-op while fresh)."""
    if not _hw_fresh():
        await asyncio.get_running_loop().run_in_executor(None, _refresh_hw_sync)


def get_hardware_engine_summary() -> dict:
    """Cached discrete GPU count and runtime engine (e.g. DUAL GPU · VULKAN).
    Async callers should `await refresh_hw_async()` first so a cold cache is
    filled in a worker thread rather than on the event loop."""
    if not _hw_fresh():
        _refresh_hw_sync()
    return dict(_hw_cache["summary"])


def _discrete_vram_totals_gb() -> list:
    if not _hw_fresh():
        _refresh_hw_sync()
    return list(_hw_cache["totals"])


def _compute_hardware_engine_summary() -> dict:
    """Calculate discrete GPU count and runtime engine (e.g. DUAL GPU · VULKAN)."""
    try:
        devs = vram.query_devices()
        igpu_kw = ("uhd", "hd graphics", "iris", "integrated", "radeon(tm) graphics")
        discrete = [d for d in devs if not any(k in d.get("name", "").lower() for k in igpu_kw)]
        count = len(discrete)
    except Exception:
        count = 0

    app_cfg = {}
    if CONFIG_FILE.exists():
        try:
            app_cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    backend = str(app_cfg.get("backend") or CONFIG_DEFAULTS.get("backend", "vulkan")).upper()

    if count == 2:
        gpu_label = "DUAL GPU"
    elif count == 1:
        gpu_label = "SINGLE GPU"
    elif count > 2:
        gpu_label = f"{count}x GPU"
    elif count == 0:
        gpu_label = "CPU"
    else:
        gpu_label = f"{count} GPU"

    tag = f"{gpu_label} · {backend}"
    return {
        "discrete_gpus": count,
        "engine": backend,
        "hardware_tag": tag,
    }


def _compute_discrete_vram_totals_gb() -> list:
    """Total VRAM (GB) of each discrete GPU, sorted largest-first, from
    `llama-bench --list-devices`. Filters out iGPUs.
    """
    try:
        devs = vram.query_devices()
        igpu_kw = ("uhd", "hd graphics", "iris", "integrated", "radeon(tm) graphics")
        totals = [
            d["total_b"] / vram.GB for d in devs
            if not any(k in d.get("name", "").lower() for k in igpu_kw)
        ]
        return sorted(totals, reverse=True)
    except Exception:
        return []


async def get_gpu_stats() -> dict:
    await refresh_hw_async()
    hw_info = get_hardware_engine_summary()
    async with _gpu_query_lock:
        if time.time() - _gpu_cache["ts"] < GPU_QUERY_INTERVAL_S and _gpu_cache["data"]["adapters"]:
            data = dict(_gpu_cache["data"])
            data["vram_totals_gb"] = _discrete_vram_totals_gb()
            data.update(hw_info)
            return data
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, _query_gpu_sync)
        data["vram_totals_gb"] = _discrete_vram_totals_gb()
        data.update(hw_info)
        _gpu_cache["ts"] = time.time()
        _gpu_cache["data"] = data
        return data
