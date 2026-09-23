import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from .backend import device_prefix
from .config import CONFIG_DEFAULTS, LLAMA_SERVER_PORT


def kill_orphan_llama_servers() -> int:
    """Kill any llama-server.exe left over from a previous/crashed manager."""
    killed = 0
    try:
        out = subprocess.run(
            ["taskkill", "/F", "/IM", "llama-server.exe", "/T"],
            capture_output=True, text=True, timeout=30,
        )
        killed = out.returncode == 0
    except Exception as e:
        print(f"[server_manager] orphan cleanup failed: {e}", file=sys.stderr)
    if killed:
        print("[server_manager] killed orphaned llama-server(s) from a previous run")
        time.sleep(2.0)  # let Windows release VRAM/DRAM
    return killed


def find_llama_server(bin_dir: str) -> Path:
    bin_dir_path = Path(bin_dir)
    for name in ("llama-server.exe", "llama-server"):
        p = bin_dir_path / name
        if p.exists():
            return p
    sys.exit(f"llama-server(.exe) not found in {bin_dir} - check 'llama_bin_dir' in the profile.")


def per_slot_cap(profile: dict) -> int:
    """--kv-unified-per-slot value to launch with, or 0 to omit it.

    The cap only splits a unified pool between several slots. With one slot
    (or a cap >= context_size) it would just shrink the only conversation —
    e.g. -c 131072 -np 1 with a 32768 cap gives a 32K window.
    """
    if not profile.get("kv_unified"):
        return 0
    cap = int(profile.get("kv_unified_per_slot") or 0)
    ctx = int(profile.get("context_size") or CONFIG_DEFAULTS["context_size"])
    n_slots = int(profile.get("n_slots") or 1)
    if cap <= 0 or n_slots <= 1 or cap >= ctx:
        return 0
    return cap


def build_launch_command(profile: dict) -> list[str]:
    bin_dir = profile.get("llama_bin_dir", CONFIG_DEFAULTS["llama_bin_dir"])
    server_bin = find_llama_server(bin_dir)
    tuned = profile.get("tuned", {})

    tensor_split = tuned.get("tensor_split") or profile.get("tensor_split", CONFIG_DEFAULTS["tensor_split"])
    n_gpu_layers = tuned.get("n_gpu_layers") if tuned.get("n_gpu_layers") is not None else profile.get("n_gpu_layers", CONFIG_DEFAULTS["n_gpu_layers"])
    split_mode = profile.get("split_mode", CONFIG_DEFAULTS["split_mode"])
    gpu_devices = profile.get("gpu_devices", CONFIG_DEFAULTS["gpu_devices"])
    backend = profile.get("backend", CONFIG_DEFAULTS["backend"])
    prefix = device_prefix(backend)

    # Zero-share segments disable that GPU: "0,1" -> only GPU #2 runs, "1,0" -> only GPU #1
    shares = [s.strip() for s in str(tensor_split).split(",")]
    if len(shares) == len(gpu_devices) and len(shares) > 1:
        gpu_devices = [d for d, s in zip(gpu_devices, shares) if s.isdigit() and int(s) > 0]
        tensor_split = ",".join(s for s in shares if int(s) > 0)

    cmd = [
        str(server_bin),
        "-m", profile["model_path"],
        "-c", str(profile.get("context_size", CONFIG_DEFAULTS["context_size"])),
        "-ngl", str(n_gpu_layers),
        "-dev", ",".join(f"{prefix}{d}" for d in gpu_devices),
        "-fa", profile.get("flash_attn", CONFIG_DEFAULTS["flash_attn"]),
        "--port", str(LLAMA_SERVER_PORT),
        "--host", "127.0.0.1",
    ]
    # split args only make sense with 2+ GPUs
    if len(gpu_devices) > 1:
        cmd += ["-sm", str(split_mode), "--tensor-split", str(tensor_split)]
    if profile.get("kv_cache_type"):
        cmd += ["-ctk", profile["kv_cache_type"], "-ctv", profile["kv_cache_type"]]
    if profile.get("threads"):
        cmd += ["-t", str(profile["threads"])]
    if profile.get("threads_batch"):
        cmd += ["-tb", str(profile["threads_batch"])]
    if profile.get("batch_size"):
        cmd += ["-b", str(profile["batch_size"])]
    if profile.get("ubatch_size"):
        cmd += ["-ub", str(profile["ubatch_size"])]
    if profile.get("n_slots"):
        cmd += ["-np", str(profile["n_slots"])]
    # llama-server only enables the unified KV pool by itself when -np is auto
    if profile.get("kv_unified"):
        cmd += ["-kvu"]
        if per_slot_cap(profile):
            cmd += ["--kv-unified-per-slot", str(per_slot_cap(profile))]
    if profile.get("cache_reuse"):
        cmd += ["--cache-reuse", str(profile["cache_reuse"])]
    if profile.get("cache_ram"):
        cmd += ["-cram", str(profile["cache_ram"])]
    if profile.get("model_type") == "moe" and tuned.get("n_cpu_moe") is not None:
        cmd += ["-ncmoe", str(tuned["n_cpu_moe"])]
    cmd += ["--jinja"]
    if profile.get("mtp_enabled") and profile.get("mtp_draft_path"):
        draft = Path(profile["mtp_draft_path"])
        if draft.exists():
            cmd += [
                "--spec-type", "draft-mtp",
                "-md", str(draft),
                "--spec-draft-n-max", str(profile.get("mtp_draft_n_max", 3)),
                "-ngld", str(profile.get("mtp_draft_ngl", "auto")),
            ]
        else:
            print(f"[server_manager] WARNING: MTP draft not found: {draft} - launching without MTP")
    # Multimodal projector: launch main model with vision support when mmproj exists
    if profile.get("vision_capable") and profile.get("mmproj_path"):
        mmproj = Path(profile["mmproj_path"])
        if mmproj.exists():
            cmd += ["--mmproj", str(mmproj)]
            print(f"[server_manager] vision_capable: loading main model with mmproj={mmproj.name}")
        else:
            print(f"[server_manager] WARNING: mmproj not found: {mmproj} - launching without vision support")
    cmd += profile.get("server_extra_args", [])
    return cmd
