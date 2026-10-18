import asyncio
import json
import shutil
import subprocess
import sys
import time
from typing import Optional

from .config import GPU_QUERY_INTERVAL_S, IGNORED_IGPU_LUIDS, CONFIG_FILE, CONFIG_DEFAULTS
from . import vram
from .process import IS_WINDOWS

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
    return _query_gpu_windows() if IS_WINDOWS else _query_gpu_linux()


def _query_gpu_linux() -> dict:
    """Same shape as the Windows query, from `llama-bench --list-devices`.
    gb = dedicated VRAM in use (matches the Windows counter); no portable
    per-process compute %, so `compute` stays empty."""
    adapters = []
    try:
        for d in vram.query_devices():
            if d.get("total_b", 0) < vram.GB:  # iGPU / software device
                continue
            adapters.append({"luid": f"vulkan{d['index']}",
                             "gb": round(d.get("used_b", 0) / vram.GB, 2)})
    except Exception as e:
        print(f"[server_manager] gpu query failed: {e}", file=sys.stderr)
    return {"adapters": adapters, "compute": []}


def _query_gpu_windows() -> dict:
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


_IGPU_NAME_KW = ("uhd", "hd graphics", "iris", "integrated", "radeon(tm) graphics",
                 "arc(tm) graphics", "xe graphics", "vega", "llvmpipe", "software")
_MIN_DISCRETE_B = 2 * vram.GB   # an iGPU/software device reports little or no dedicated memory


def _discrete_devices(devs: list) -> list:
    """The discrete GPUs in a `llama-bench --list-devices` list. One rule for the count, the
    VRAM totals and the per-GPU rows, so they cannot disagree about what counts as a GPU."""
    return [d for d in devs
            if d.get("total_b", 0) >= _MIN_DISCRETE_B
            and not any(k in d.get("name", "").lower() for k in _IGPU_NAME_KW)]


def _compute_hardware_engine_summary() -> dict:
    """Calculate discrete GPU count and runtime engine (e.g. DUAL GPU · VULKAN)."""
    try:
        discrete = _discrete_devices(vram.query_devices())
        count = len(discrete)
    except Exception:
        discrete, count = [], 0

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
        # index = the id used in gpu_devices / -dev; lets the UI label one tensor-split row per GPU
        "devices": [{"index": d["index"], "name": d.get("name", ""),
                     "total_gb": round(d["total_b"] / vram.GB, 1)} for d in discrete],
        "engine": backend,
        "hardware_tag": tag,
    }


def _compute_discrete_vram_totals_gb() -> list:
    """Total VRAM (GB) of each discrete GPU, sorted largest-first, from
    `llama-bench --list-devices`. Filters out iGPUs.
    """
    try:
        totals = [d["total_b"] / vram.GB for d in _discrete_devices(vram.query_devices())]
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


# --- real GPU temperature/power --------------------------------------------

PS_LHM_SENSOR_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$sensors = Get-CimInstance -Namespace root/LibreHardwareMonitor -ClassName Sensor |
  Where-Object { $_.Identifier -like '/gpu/*' -and ($_.SensorType -eq 'Temperature' -or $_.SensorType -eq 'Power') }
$rows = @()
foreach ($s in $sensors) {
  $rows += [pscustomobject]@{ name = $s.Name; type = $s.SensorType; value = [double] $s.Value }
}
[pscustomobject]@{ source = 'lhm'; gpus = $rows } | ConvertTo-Json -Compress -Depth 3
"""

# A negative probe is cached long: sensors don't appear because the dashboard
# polls. Re-running the WMI/console probe once a second proved nothing only
# returns None slower.
SENSOR_FAIL_TTL_S = 300.0
SENSOR_OK_TTL_S = 3.0
_sensor_cache = {"ts": 0.0, "data": None}
_sensor_lock = asyncio.Lock()


def _query_nvidia_sensors_sync() -> list:
    """[{gpu, temp_c, watts}] from nvidia-smi, [] when it fails or has none."""
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,temperature.gpu,power.draw",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode != 0:
            return []
        rows = []
        for line in (proc.stdout or "").strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 3:
                continue
            try:
                temp = float(parts[1]) if parts[1] not in ("N/A", "") else None
                watts = float(parts[2]) if parts[2] not in ("N/A", "") else None
                rows.append({"gpu": int(float(parts[0])), "temp_c": temp, "watts": watts})
            except (TypeError, ValueError):
                continue
        return rows
    except (OSError, subprocess.SubprocessError):
        return []


_last_intel_energy: dict[int, tuple[int, int]] = {}


def _query_intel_sensors_sync() -> list:
    """[{gpu, name, temp_c, watts}] from Intel Level Zero Sysman (ze_loader.dll / libze_loader.so)."""
    import ctypes
    lib_name = "ze_loader.dll" if IS_WINDOWS else "libze_loader.so"
    try:
        ze = ctypes.CDLL(lib_name)
    except (OSError, Exception):
        return []

    try:
        if not hasattr(ze, "zesInit"):
            return []
        ze.zesInit.argtypes = [ctypes.c_uint32]
        ze.zesInit.restype = ctypes.c_int32
        if ze.zesInit(0) != 0:
            return []

        ze.zesDriverGet.argtypes = [ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_void_p)]
        ze.zesDriverGet.restype = ctypes.c_int32
        ze.zesDeviceGet.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_void_p)]
        ze.zesDeviceGet.restype = ctypes.c_int32
        ze.zesDeviceEnumTemperatureSensors.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_void_p)]
        ze.zesDeviceEnumTemperatureSensors.restype = ctypes.c_int32
        ze.zesTemperatureGetState.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_double)]
        ze.zesTemperatureGetState.restype = ctypes.c_int32
        ze.zesDeviceEnumPowerDomains.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_void_p)]
        ze.zesDeviceEnumPowerDomains.restype = ctypes.c_int32

        class _ZesEnergyCounter(ctypes.Structure):
            _fields_ = [("energy", ctypes.c_uint64), ("timestamp", ctypes.c_uint64)]

        ze.zesPowerGetEnergyCounter.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ZesEnergyCounter)]
        ze.zesPowerGetEnergyCounter.restype = ctypes.c_int32

        count = ctypes.c_uint32(0)
        if ze.zesDriverGet(ctypes.byref(count), None) != 0 or count.value == 0:
            return []
        drivers = (ctypes.c_void_p * count.value)()
        ze.zesDriverGet(ctypes.byref(count), drivers)

        rows = []
        gpu_idx = 0
        for drv in drivers:
            dev_count = ctypes.c_uint32(0)
            if ze.zesDeviceGet(drv, ctypes.byref(dev_count), None) != 0:
                continue
            devs = (ctypes.c_void_p * dev_count.value)()
            ze.zesDeviceGet(drv, ctypes.byref(dev_count), devs)

            for dev in devs:
                temp_c = None
                t_count = ctypes.c_uint32(0)
                if ze.zesDeviceEnumTemperatureSensors(dev, ctypes.byref(t_count), None) == 0 and t_count.value > 0:
                    t_handles = (ctypes.c_void_p * t_count.value)()
                    ze.zesDeviceEnumTemperatureSensors(dev, ctypes.byref(t_count), t_handles)
                    temps = []
                    for th in t_handles:
                        t_val = ctypes.c_double(0.0)
                        if ze.zesTemperatureGetState(th, ctypes.byref(t_val)) == 0 and t_val.value > 0:
                            temps.append(float(t_val.value))
                    if temps:
                        temp_c = round(max(temps), 1)

                watts = None
                p_count = ctypes.c_uint32(0)
                if ze.zesDeviceEnumPowerDomains(dev, ctypes.byref(p_count), None) == 0 and p_count.value > 0:
                    p_handles = (ctypes.c_void_p * p_count.value)()
                    ze.zesDeviceEnumPowerDomains(dev, ctypes.byref(p_count), p_handles)
                    ec = _ZesEnergyCounter()
                    if ze.zesPowerGetEnergyCounter(p_handles[0], ctypes.byref(ec)) == 0:
                        now_e, now_t = int(ec.energy), int(ec.timestamp)
                        prev = _last_intel_energy.get(gpu_idx)
                        if not prev:
                            time.sleep(0.04)
                            ec2 = _ZesEnergyCounter()
                            if ze.zesPowerGetEnergyCounter(p_handles[0], ctypes.byref(ec2)) == 0:
                                dt = int(ec2.timestamp) - now_t
                                de = int(ec2.energy) - now_e
                                if dt > 0:
                                    watts = round(de / dt, 1)
                                now_e, now_t = int(ec2.energy), int(ec2.timestamp)
                        else:
                            prev_e, prev_t = prev
                            dt = now_t - prev_t
                            de = now_e - prev_e
                            if dt > 0:
                                watts = round(de / dt, 1)
                        _last_intel_energy[gpu_idx] = (now_e, now_t)

                if temp_c is not None or watts is not None:
                    rows.append({"gpu": gpu_idx, "name": f"Intel Arc #{gpu_idx}", "temp_c": temp_c, "watts": watts})
                    gpu_idx += 1
        return rows
    except Exception:
        return []


def query_gpu_sensors_sync() -> Optional[dict]:
    """Real GPU temperature/power readings, or None when this machine offers
    no source. Order: Intel Level Zero Sysman (Intel Arc) -> nvidia-smi (CUDA rig) -> LibreHardwareMonitor WMI (if
    the operator runs it). Nothing is modeled; an unknown stack gets None and
    the UI shows N/A instead of a thermometer drawn from the load percent."""
    intel_rows = _query_intel_sensors_sync()
    if intel_rows:
        return {"source": "intel-sysman", "gpus": intel_rows}
    if IS_WINDOWS and shutil.which("nvidia-smi"):
        rows = _query_nvidia_sensors_sync()
        if rows:
            return {"source": "nvidia-smi", "gpus": rows}
    if IS_WINDOWS:
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                 PS_LHM_SENSOR_SCRIPT],
                capture_output=True, text=True, timeout=10,
            )
            data = json.loads(proc.stdout.strip() or "null")
            gpus = data.get("gpus") or [] if isinstance(data, dict) else []
            # normalize to the same shape nvidia-smi rows use
            rows = []
            for g in gpus:
                if not isinstance(g, dict) or g.get("value") is None:
                    continue
                t = float(g["value"]) if g.get("type") == "Temperature" else None
                w = float(g["value"]) if g.get("type") == "Power" else None
                if t is None and w is None:
                    continue
                rows.append({"name": g.get("name", ""), "temp_c": t, "watts": w})
            if rows:
                return {"source": "lhm", "gpus": rows}
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            pass
    return None


def _sensor_cache_fresh() -> bool:
    ttl = SENSOR_OK_TTL_S if _sensor_cache["data"] is not None else SENSOR_FAIL_TTL_S
    return time.time() - _sensor_cache["ts"] < ttl


async def get_gpu_sensors() -> Optional[dict]:
    """Cached real-sensor probe for the telemetry snapshot (None = no source)."""
    if _sensor_cache_fresh():
        return _sensor_cache["data"]
    async with _sensor_lock:
        if _sensor_cache_fresh():
            return _sensor_cache["data"]
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, query_gpu_sensors_sync)
        _sensor_cache["data"] = data
        _sensor_cache["ts"] = time.time()
        return data
