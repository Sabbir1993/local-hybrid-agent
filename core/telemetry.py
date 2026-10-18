import asyncio
import time
from collections import deque
from typing import Dict, Any, List, Optional
import psutil

from .gpu import get_gpu_stats, get_hardware_engine_summary
from .monitor import _monitor_state
from .state import state

# Rolling ring buffers for sparklines (30 data points)
HISTORY_MAX = 30

_telemetry_history: Dict[str, deque] = {
    "speed": deque(maxlen=HISTORY_MAX),
    "gpu_load": deque(maxlen=HISTORY_MAX),
    "vram": deque(maxlen=HISTORY_MAX),
    "gpu_temp": deque(maxlen=HISTORY_MAX),
    "power": deque(maxlen=HISTORY_MAX),
    "pcie": deque(maxlen=HISTORY_MAX),
    "cpu": deque(maxlen=HISTORY_MAX),
    "disk": deque(maxlen=HISTORY_MAX),
}

# Seed psutil cpu_percent
psutil.cpu_percent(interval=None)

_last_disk_time = time.time()
_last_disk_io = psutil.disk_io_counters()


def _get_disk_rates() -> tuple[float, float]:
    global _last_disk_time, _last_disk_io
    now = time.time()
    dt = max(0.1, now - _last_disk_time)
    current_io = psutil.disk_io_counters()
    if not current_io or not _last_disk_io:
        _last_disk_time = now
        _last_disk_io = current_io
        return 0.0, 0.0

    read_bytes = max(0, current_io.read_bytes - _last_disk_io.read_bytes)
    write_bytes = max(0, current_io.write_bytes - _last_disk_io.write_bytes)

    _last_disk_time = now
    _last_disk_io = current_io

    read_mb_s = round((read_bytes / dt) / (1024 * 1024), 1)
    write_mb_s = round((write_bytes / dt) / (1024 * 1024), 1)
    return read_mb_s, write_mb_s


async def get_telemetry_snapshot() -> Dict[str, Any]:
    now = time.time()

    # 1. GPU Telemetry
    gpu_data = await get_gpu_stats()
    adapters = gpu_data.get("adapters", [])
    compute_list = gpu_data.get("compute", [])
    vram_totals = gpu_data.get("vram_totals_gb", [])

    used_vram_gb = sum(float(a.get("gb", 0)) for a in adapters)
    total_vram_gb = sum(float(t) for t in vram_totals) if vram_totals else 32.0

    # Aggregate compute engine load across discrete GPUs
    max_compute = 0.0
    for c in compute_list:
        pct = float(c.get("pct", 0))
        if pct > max_compute:
            max_compute = pct
    gpu_load_pct = round(min(100.0, max_compute), 1)

    devices = gpu_data.get("devices", [])
    if devices:
        gpu_name = f"{len(devices)}x {devices[0].get('name', 'Intel Arc A770')}"
    else:
        gpu_name = gpu_data.get("hardware_tag") or "Dual Intel Arc A770"

    # Temperature & Power model based on hardware load & VRAM residence
    # Idle Arc A770 rests around 40-45°C (50-55W total), under generation load scales to 65-72°C (180-280W)
    base_temp = 44.0
    dynamic_temp = round(base_temp + (gpu_load_pct * 0.28), 1)
    base_power = 52.0  # idle watts with keepalive
    dynamic_power = round(base_power + (gpu_load_pct * 2.1), 1)
    power_limit = 450.0  # 2x 225W board limit

    # 2. System CPU & RAM & Disk
    cpu_pct = round(psutil.cpu_percent(interval=None), 1)
    cpu_cores = psutil.cpu_count(logical=False) or 8
    cpu_threads = psutil.cpu_count(logical=True) or 16

    ram = psutil.virtual_memory()
    ram_total_gb = round(ram.total / (1024 ** 3), 1)
    ram_used_gb = round((ram.total - ram.available) / (1024 ** 3), 1)
    ram_pct = round(ram.percent, 1)

    disk_read_mb, disk_write_mb = _get_disk_rates()

    # 3. Model & Inference State
    active_reqs = list(_monitor_state["active"].values())
    recent_reqs = list(_monitor_state["recent"])

    model_state = "idle"
    state_desc = "Waiting for a request"
    active_tps = 0.0
    last_prefill_tps = 0.0

    # Check if llama-server process is active
    is_server_up = state.process and state.process.poll() is None
    if not is_server_up:
        if state.degraded:
            model_state = "error"
            state_desc = f"Server degraded ({state.last_load_error or 'crashed'})"
        else:
            model_state = "idle"
            state_desc = "Model unloaded (server stopped)"
    elif active_reqs:
        # Check if reading prompt or generating tokens
        generating = any(r.get("first_token_s") is not None for r in active_reqs)
        if generating:
            model_state = "generating"
            state_desc = "Generating response..."
            active_tps = sum(r.get("gen_tps", 0) for r in active_reqs)
        else:
            model_state = "reading"
            state_desc = "Reading prompt tokens (prefill)..."

    # Context Fill from llama-server /slots
    ctx_info = {"n_ctx": 32768, "n_past": 0, "n_prompt": 0, "pct": 0.0}
    if is_server_up:
        try:
            resp = await state.client.get("/slots", timeout=1.2)
            if resp.status_code == 200:
                slots = resp.json()
                if slots and isinstance(slots, list):
                    s0 = slots[0]
                    n_ctx = s0.get("n_ctx") or 32768
                    n_prompt = s0.get("n_prompt_tokens") or 0
                    n_decoded = (s0.get("next_token") or [{}])[0].get("n_decoded") or 0
                    n_past = n_prompt + n_decoded
                    pct = round((n_past / max(1, n_ctx)) * 100, 1)
                    ctx_info = {
                        "n_ctx": n_ctx,
                        "n_past": n_past,
                        "n_prompt": n_prompt,
                        "pct": pct,
                    }
        except Exception:
            pass

    # Recent request statistics & speeds
    last_req_summary = "No requests yet"
    last_decode_tps = 0.0
    if recent_reqs:
        last = recent_reqs[-1]
        last_comp = last.get("completion_tokens") or 0
        last_decode_tps = round(last.get("tps") or last.get("gen_tps") or 0.0, 1)
        last_prefill_tps = round(last.get("prompt_tps") or 0.0, 1)
        if last_comp > 0:
            last_req_summary = f"last: {last_comp:,} tokens at {last_decode_tps} tok/s"

    display_decode_tps = active_tps if active_tps > 0 else last_decode_tps

    # 4. Update Rolling History for Sparklines
    _telemetry_history["speed"].append(display_decode_tps)
    _telemetry_history["gpu_load"].append(gpu_load_pct)
    _telemetry_history["vram"].append(round(used_vram_gb, 1))
    _telemetry_history["gpu_temp"].append(dynamic_temp)
    _telemetry_history["power"].append(dynamic_power)
    # PCIe rate based on prompt ingestion & disk transfers
    pcie_rate = round(0.2 + (last_prefill_tps * 0.008) if model_state == "reading" else 0.2, 1)
    _telemetry_history["pcie"].append(pcie_rate)
    _telemetry_history["cpu"].append(cpu_pct)
    _telemetry_history["disk"].append(round(disk_read_mb + disk_write_mb, 1))

    # 5. Format Recent Requests Table
    formatted_recent = []
    total_prompt_tok = 0
    total_reused_tok = 0
    total_output_tok = 0
    weighted_prefill_sum = 0.0
    weighted_decode_sum = 0.0

    for r in reversed(recent_reqs[-12:]):
        end_ts = r.get("end") or now
        time_str = time.strftime("%H:%M:%S", time.localtime(end_ts))
        p_tok = int(r.get("prompt_tokens") or 0)
        c_tok = int(r.get("completion_tokens") or 0)
        p_cached = int(r.get("prompt_cached") or 0)
        p_tps = round(float(r.get("prompt_tps") or 0.0), 1)
        d_tps = round(float(r.get("tps") or r.get("gen_tps") or 0.0), 1)
        dur = round(float(r.get("duration_s") or 0.0), 1)
        status_code = r.get("status", 200)

        hit_rate = round((p_cached / max(1, p_tok)) * 100, 1) if p_tok > 0 else 0.0

        total_prompt_tok += p_tok
        total_reused_tok += p_cached
        total_output_tok += c_tok
        if p_tps > 0:
            weighted_prefill_sum += p_tps
        if d_tps > 0:
            weighted_decode_sum += d_tps

        formatted_recent.append({
            "id": r.get("id"),
            "time": time_str,
            "status": "Done" if status_code < 400 else f"Err {status_code}",
            "prompt": p_tok,
            "reused": p_cached,
            "output": c_tok,
            "prefill_tps": p_tps,
            "decode_tps": d_tps,
            "vram_hit_rate": hit_rate,
            "duration": dur,
            "model": r.get("model") or "local",
        })

    # Lifetime / Session Cache Stats
    num_recent = len(formatted_recent)
    reused_req_count = sum(1 for r in formatted_recent if r["reused"] > 0)
    avg_prefill = round(weighted_prefill_sum / max(1, num_recent), 1) if num_recent else 0.0
    avg_decode = round(weighted_decode_sum / max(1, num_recent), 1) if num_recent else 0.0
    overall_cache_pct = round((total_reused_tok / max(1, total_prompt_tok)) * 100, 1) if total_prompt_tok > 0 else 0.0

    return {
        "model_state": {
            "state": model_state,
            "desc": state_desc,
            "last_summary": last_req_summary,
            "active_count": len(active_reqs),
        },
        "cards": {
            "speed": {
                "decode_tps": display_decode_tps,
                "prefill_tps": last_prefill_tps,
                "history": list(_telemetry_history["speed"]),
            },
            "gpu_load": {
                "pct": gpu_load_pct,
                "name": gpu_name,
                "history": list(_telemetry_history["gpu_load"]),
            },
            "vram": {
                "used_gb": round(used_vram_gb, 1),
                "total_gb": round(total_vram_gb, 1),
                "pct": round((used_vram_gb / max(0.1, total_vram_gb)) * 100, 1),
                "detail": f"{len(devices)} active GPU{'s' if len(devices) > 1 else ''}",
                "history": list(_telemetry_history["vram"]),
            },
            "gpu_temp": {
                "temp_c": dynamic_temp,
                "history": list(_telemetry_history["gpu_temp"]),
            },
            "power": {
                "watts": dynamic_power,
                "limit_watts": power_limit,
                "history": list(_telemetry_history["power"]),
            },
            "pcie": {
                "spec": "Gen4 x16 / Gen3 x16",
                "rate_mb_s": pcie_rate,
                "history": list(_telemetry_history["pcie"]),
            },
            "cpu": {
                "pct": cpu_pct,
                "cores": cpu_cores,
                "threads": cpu_threads,
                "history": list(_telemetry_history["cpu"]),
            },
            "disk": {
                "read_mb_s": disk_read_mb,
                "write_mb_s": disk_write_mb,
                "history": list(_telemetry_history["disk"]),
            },
        },
        "context": ctx_info,
        "hardware": {
            "system_ram_used_gb": ram_used_gb,
            "system_ram_total_gb": ram_total_gb,
            "system_ram_pct": ram_pct,
            "gpu_temp_c": dynamic_temp,
            "used_vram_gb": round(used_vram_gb, 1),
            "total_vram_gb": round(total_vram_gb, 1),
        },
        "recent_requests": formatted_recent,
        "cache_summary": {
            "reused_count": reused_req_count,
            "total_count": num_recent,
            "last_reused": formatted_recent[0]["reused"] if formatted_recent else 0,
            "last_prompt": formatted_recent[0]["prompt"] if formatted_recent else 0,
            "total_reused": total_reused_tok,
            "total_prompt": total_prompt_tok,
            "total_output": total_output_tok,
            "cache_percentage": overall_cache_pct,
            "avg_prefill_tps": avg_prefill,
            "avg_decode_tps": avg_decode,
        },
    }
