import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from .backend import build_child_env
from .config import (ACTIVE_RUNTIME, CONFIG_DEFAULTS, LLAMA_SERVER_PORT, apply_runtime,
                    normalize_tensor_split)
from .vram.devices import resolve_device_ids

IS_WINDOWS = os.name == "nt"
MAIN_CACHE_RAM_MB = 2048     # -cram when a profile sets none (llama's own default is 8192 MiB of host RAM)


def kill_process_tree(proc: Optional[subprocess.Popen], timeout: float = 15) -> None:
    """Force-stop a child server; never raises. Windows: taskkill /T (tree),
    elsewhere terminate -> wait -> kill (llama/whisper/sd servers spawn no children).
    A vLLM leader that already exited (crashed) can leave GPU workers behind: its group is still stopped (remember_engine_pid
    marked it)."""
    if proc is None:
        return
    if proc.poll() is not None:
        rec = getattr(proc, "_engine_group", None)
        if isinstance(rec, dict) and not IS_WINDOWS:
            try:
                _kill_group(rec["pgid"], rec.get("start"), grace=min(timeout, 5.0))
            except Exception:
                pass
        return
    group = not IS_WINDOWS and _own_group(proc)       # read now: once the leader is reaped its group cannot be looked up
    if IS_WINDOWS:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=timeout)
        except Exception:
            pass
    else:
        try:
            if group:
                os.killpg(proc.pid, signal.SIGTERM)     # vLLM: the server and its per-GPU workers
            else:
                proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout=timeout)
    except Exception:
        try:
            if group:
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass
    if group:
        try:
            os.killpg(proc.pid, signal.SIGKILL)         # workers that outlived the server would keep their VRAM
        except Exception:
            pass


def _own_group(proc) -> bool:
    """True when proc leads its own process group (started with start_new_session=True)."""
    try:
        return os.getpgid(proc.pid) == proc.pid
    except Exception:
        return False


# The vLLM server is a Python program with worker processes, so it cannot be found by name like llama-server. Its process
# group id and its start time are kept in this file while it runs, and a restart of the app stops a group left behind by a
# crash. The start time (plus the boot id) guards against a recycled process id: a group is only stopped when it is the one
# that was recorded.
ENGINE_PID_FILE = Path(__file__).resolve().parent.parent / "logs" / "main-engine.pid"


def _stat_fields(pid: int):
    """/proc/<pid>/stat fields after the command name (state, ppid, pgrp, session, ... starttime at [19]), or None."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(errors="replace")
        return raw.rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return None


def _proc_start(pid: int):
    f = _stat_fields(pid)
    try:
        return int(f[19]) if f else None
    except (ValueError, IndexError):
        return None


def _group_members(pgid: int) -> list:
    """[(pid, start_time)] of the live (not zombie) processes in process group pgid. Linux /proc only; [] elsewhere."""
    out = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return out
    for n in names:
        if not n.isdigit():
            continue
        f = _stat_fields(int(n))
        try:
            if f and f[0] != "Z" and int(f[2]) == pgid:
                out.append((int(n), int(f[19])))
        except (ValueError, IndexError):
            continue
    return out


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def _cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return ""


def _kill_group(pgid: int, start, grace: float = 5.0) -> bool:
    """SIGTERM, then SIGKILL after `grace` seconds, the process group pgid - but only when it is the recorded vLLM group.
    start = the leader's start time as recorded (None = unknown, an old pid file; then a live leader must say "vllm").
    While any process is in a group its id cannot be given to a new process, so a live /proc/<pgid> whose start time differs
    is a recycled id of an unrelated process, and workers (started after the leader) can only be ours."""
    if pgid <= 1 or pgid == os.getpgrp():
        return False
    members = _group_members(pgid)
    if not members:
        return False
    leader_start = _proc_start(pgid)
    if leader_start is not None:                              # the leader is alive (or its id was recycled)
        if start is not None:
            if leader_start != start:
                return False
        elif "vllm" not in _cmdline(pgid).lower():
            return False
    elif start is None:                                       # leader gone and nothing recorded to prove whose workers
        if not any("vllm" in _cmdline(pid).lower() for pid, _ in members):
            return False
    else:
        members = [m for m in members if m[1] >= start]       # a worker was started after its leader
        if not members:
            return False
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return False
    deadline = time.monotonic() + max(0.0, grace)
    while time.monotonic() < deadline and _group_members(pgid):
        time.sleep(0.1)
    try:
        os.killpg(pgid, signal.SIGKILL)                       # workers that ignore SIGTERM would keep their VRAM
    except (ProcessLookupError, PermissionError):
        pass
    return True


def remember_engine_pid(proc) -> None:
    pgid = proc.pid
    rec = {"pgid": pgid, "start": None if IS_WINDOWS else _proc_start(pgid), "boot": "" if IS_WINDOWS else _boot_id()}
    try:
        proc._engine_group = rec            # kill_process_tree needs it after the leader has exited and cannot be looked up
    except AttributeError:
        pass
    try:
        ENGINE_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        ENGINE_PID_FILE.write_text(json.dumps(rec))
    except OSError:
        pass


def forget_engine_pid() -> None:
    try:
        ENGINE_PID_FILE.unlink()
    except OSError:
        pass


def _read_engine_pid():
    """The pid file as {"pgid", "start", "boot"}; an old file holds just the pid (start unknown). None if unreadable."""
    try:
        txt = ENGINE_PID_FILE.read_text().strip()
    except OSError:
        return None
    try:
        rec = json.loads(txt)
        if isinstance(rec, int):
            rec = {"pgid": rec}
        return {"pgid": int(rec["pgid"]), "start": rec.get("start"), "boot": rec.get("boot") or ""}
    except (ValueError, KeyError, TypeError):
        return None


def kill_orphan_engine() -> bool:
    """Stop a vLLM process group recorded by a previous run of the app, if it is still that group: also when the server
    process itself died and only its GPU workers are left (they keep their VRAM)."""
    rec = _read_engine_pid()
    if rec is None:
        forget_engine_pid()
        return False
    killed = False
    if not IS_WINDOWS:
        try:
            same_boot = not rec["boot"] or rec["boot"] == _boot_id()      # after a reboot an id means something else
            if same_boot and _kill_group(rec["pgid"], rec["start"], grace=5.0):
                killed = True
                print("[server_manager] stopped a vLLM server left over from a previous run")
        except (OSError, ValueError):
            pass
    forget_engine_pid()
    return killed


def kill_orphan_llama_servers() -> int:
    """Kill any llama-server(.exe) left over from a previous/crashed manager."""
    killed = 0
    try:
        cmd = (["taskkill", "/F", "/IM", "llama-server.exe", "/T"] if IS_WINDOWS
               else ["pkill", "-x", "llama-server"])
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
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
    raise RuntimeError(
        f"llama-server(.exe) not found in {bin_dir} (runtime '{ACTIVE_RUNTIME['name']}') - "
        f"check runtimes.{ACTIVE_RUNTIME['name']}.llama_bin_dir in config/app.json.")


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


def build_launch_environment(profile: dict, *, base_env=None) -> dict:
    """Child environment matching the effective devices chosen for launch."""
    devices = profile.get("_launch_gpu_devices")
    if devices is None:
        devices = profile.get("gpu_devices") or CONFIG_DEFAULTS["gpu_devices"]
    return build_child_env(
        profile.get("backend", CONFIG_DEFAULTS["backend"]),
        runtime_env=profile.get("runtime_env"),
        device_indices=list(devices),
        base_env=base_env,
        set_visibility=False,
    )


_SHOW_ENV = ("SYCL", "ONEAPI", "ZE_", "UR_", "GGML", "LD_", "OMP_", "MKL_", "TBB", "NEO", "IGC", "HOME", "XDG_RUNTIME")


def describe_launch_env(env: dict, cwd: Optional[str] = None) -> str:
    """One log line saying what a llama-server child is given, for crashes that only happen under the app.

    Values are shown only for the GPU-runtime variables (no secrets live there); every other variable is counted by
    name only. Also lists the other llama-server processes alive at that moment (pid:port), because the SYCL runtime
    is sensitive to several starting on the same cards.
    """
    shown = {k: v for k, v in sorted(env.items()) if k.startswith(_SHOW_ENV)}
    others = []
    try:
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                c = Path(f"/proc/{d}/cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            if c and Path(c[0].decode(errors="replace")).name.startswith("llama-server"):
                port = c[c.index(b"--port") + 1].decode(errors="replace") if b"--port" in c and c.index(b"--port") + 1 < len(c) else "?"
                others.append(f"{d}:{port}")
    except OSError:
        pass
    return (f"cwd={cwd or os.getcwd()} env_vars={len(env)} gpu_env={shown} "
            f"other_llama_servers={others or 'none'}")


def exit_signal_note(code) -> str:
    """' = SIGSEGV' for a negative exit code, so a crash reads as one."""
    try:
        return f" = {signal.Signals(-int(code)).name}" if int(code) < 0 else ""
    except (ValueError, TypeError):
        return ""


def build_launch_command(profile: dict) -> list[str]:
    apply_runtime(profile)
    bin_dir = profile.get("llama_bin_dir", CONFIG_DEFAULTS["llama_bin_dir"])
    server_bin = find_llama_server(bin_dir)
    tuned = profile.get("tuned", {})

    tensor_split = tuned.get("tensor_split") or profile.get("tensor_split", CONFIG_DEFAULTS["tensor_split"])
    n_gpu_layers = tuned.get("n_gpu_layers") if tuned.get("n_gpu_layers") is not None else profile.get("n_gpu_layers", CONFIG_DEFAULTS["n_gpu_layers"])
    # MoE models are split by experts, not by layer (see core/vram/moe.py)
    from .vram.gguf_parser import parse_gguf_info
    from .vram.moe import is_moe, resolve_offload
    gguf = parse_gguf_info(profile["model_path"])
    n_gpu_layers, n_cpu_moe = resolve_offload(
        profile, gguf, int(n_gpu_layers),
        tuned.get("n_cpu_moe"), profile.get("_auto_n_cpu_moe"))
    split_mode = profile.get("split_mode", CONFIG_DEFAULTS["split_mode"])
    gpu_devices = profile.get("gpu_devices", CONFIG_DEFAULTS["gpu_devices"])
    backend = profile.get("backend", CONFIG_DEFAULTS["backend"])

    # Zero-share segments disable that GPU: "0,1" -> only GPU #2 runs, "1,0" -> only GPU #1.
    # Non-numeric segments are dropped like the zero-shares are (same guard as the line
    # above): a hand-edited "9," used to die here on int("") while the device list built
    # one line up silently dropped the same segment.
    # Shared with the VRAM estimator (core/config.normalize_tensor_split) so the config that
    # gets CHECKED is the config that gets LAUNCHED - they used to filter segments in a
    # different order and disagreed on exactly the malformed inputs this guards against.
    gpu_devices, tensor_split = normalize_tensor_split(tensor_split, gpu_devices)
    device_ids = resolve_device_ids(
        gpu_devices,
        backend=backend,
        llama_bin_dir=bin_dir,
        runtime_env=profile.get("runtime_env"),
        strict=False,
    )
    # state.py builds the child environment after this function returns. Keep
    # the post-zero-share list so visibility and -dev can never disagree.
    profile["_launch_gpu_devices"] = list(gpu_devices)
    profile["_launch_device_ids"] = list(device_ids)

    cmd = [
        str(server_bin),
        "-m", profile["model_path"],
        "-c", str(profile.get("context_size", CONFIG_DEFAULTS["context_size"])),
        "-ngl", str(n_gpu_layers),
        "-dev", ",".join(device_ids),
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
    # an unset/0 profile value used to mean llama's own 8192 MiB host-RAM prompt cache: bound it (-1 = no limit)
    cmd += ["-cram", str(profile.get("cache_ram") or MAIN_CACHE_RAM_MB)]
    if n_cpu_moe > 0:
        cmd += ["-ncmoe", str(n_cpu_moe)]
    # Expert weights left in RAM (a partial -ngl or -ncmoe): llama.cpp normally copies them to the
    # GPU for long prompts (op offload) and the Vulkan build aborts there with
    # GGML_ASSERT(id >= 0 && id < n_expert) - short warm-up prompts pass, the first real one
    # kills the server. Keeping those ops on the CPU is slower for prompt processing but stable.
    op_offload = profile.get("op_offload")
    experts_in_ram = is_moe(profile, gguf) and (n_cpu_moe > 0 or (
        gguf and 0 < n_gpu_layers < int(gguf.get("n_layer") or 0) + 1))
    if op_offload is False or (op_offload is None and experts_in_ram):
        cmd += ["--no-op-offload"]
    # --load-mode none (no mmap) crashes the SYCL build with a general protection fault in libc while the weights are
    # copied (llama-server exit -11); the same model loads fine without it, so SYCL always keeps the default
    if profile.get("use_mmap") is False and str(backend) != "sycl":
        cmd += ["--load-mode", "none"]
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
