"""Several llama-server instances for the main model, each on its own GPU group.

Configuration only: validation, port layout and the list of servers to launch. No process or HTTP code lives here.
"""
import math

from .config import LLAMA_SERVER_PORT

MAX_INSTANCES = 8
# Instance 0 keeps the classic main port; instances 1..7 get 8191..8197. Helper lanes must stay out of this block
# (core/lanes/local_storage.py), so the range is reserved rather than just "whatever is free".
EXTRA_PORT_BASE = 8190
RESERVED_PORTS = range(8190, 8200)
MAX_SLOTS = 64


def instance_port(index: int) -> int:
    return LLAMA_SERVER_PORT if index == 0 else EXTRA_PORT_BASE + index


def saved_instances(profile: dict) -> list:
    """The configured instance list. "tuned" wins while a profile is loaded; the top-level copy is what
    a model's saved config carries before apply_runtime() runs."""
    tuned = profile.get("tuned") or {}
    raw = tuned.get("main_instances") or profile.get("main_instances") or []
    return raw if isinstance(raw, list) else []


def main_mode(profile: dict) -> str:
    tuned = profile.get("tuned") or {}
    mode = tuned.get("main_mode") or profile.get("main_mode")
    if mode != "multi" or profile.get("engine") == "vllm":
        return "single"
    return "multi" if saved_instances(profile) else "single"


def small_gpu_warnings(instances: list, small_gpu) -> list:
    """Notes for instances that share a GPU with the helper-model card. Not an error: the helpers just compete for VRAM."""
    if small_gpu is None:
        return []
    out = []
    for i, item in enumerate(instances or [], start=1):
        gpus = item.get("gpus") if isinstance(item, dict) else None
        if isinstance(gpus, list) and int(small_gpu) in [g for g in gpus if isinstance(g, int)]:
            out.append(f"Instance {i} uses GPU {int(small_gpu)}, which the helper models also use.")
    return out


def _share_list(text: str, count: int, i: int) -> list:
    parts = [s.strip() for s in str(text).split(",")]
    try:
        shares = [float(s) for s in parts]
    except ValueError:
        shares = []
    if len(shares) != count or any(not math.isfinite(v) or v < 0 for v in shares):
        raise ValueError(f"Server {i}: tensor_split needs {count} comma-separated non-negative numbers, one per GPU")
    if not any(v > 0 for v in shares):
        raise ValueError(f"Server {i}: at least one tensor_split share must be above 0")
    return parts


def validate_instances(raw, runtime_gpus, engine="llama", small_gpu=None) -> tuple:
    """(clean_list, warnings) for the admin's server list. Raises ValueError with one user-facing sentence."""
    if engine == "vllm":
        raise ValueError("Multiple servers work with the llama.cpp engine only")
    if not isinstance(raw, list):
        raise ValueError("main_instances must be a list of servers")
    if not raw:
        raise ValueError("Add at least one server")
    if len(raw) > MAX_INSTANCES:
        raise ValueError(f"At most {MAX_INSTANCES} servers can run the main model")
    runtime = {int(d) for d in (runtime_gpus or [])}
    owner: dict = {}      # gpu -> 1-based instance number that uses it
    clean = []
    for i, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Server {i} must be an object with gpus and n_slots")
        gpus_in = item.get("gpus")
        if not isinstance(gpus_in, list) or not gpus_in:
            raise ValueError(f"Server {i} needs at least one GPU")
        gpus = []
        for g in gpus_in:
            if isinstance(g, bool) or not isinstance(g, int):
                raise ValueError(f"Server {i}: GPU numbers must be whole numbers")
            if g not in runtime:
                raise ValueError(f"Server {i}: GPU {g} is not in the active runtime's GPU list")
            if g in gpus:
                raise ValueError(f"Server {i} lists GPU {g} more than once")
            if g in owner:
                raise ValueError(f"GPU {g} is used by servers {owner[g]} and {i}; each GPU can serve one server")
            owner[g] = i
            gpus.append(g)
        n = item.get("n_slots")
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= MAX_SLOTS:
            raise ValueError(f"Server {i}: n_slots must be a whole number from 1 to {MAX_SLOTS}")
        ts = item.get("tensor_split")
        shares = None
        if len(gpus) > 1 and ts not in (None, ""):
            shares = _share_list(ts, len(gpus), i)
        # the split is positional, so sort GPUs and shares together, never separately
        order = sorted(range(len(gpus)), key=lambda k: gpus[k])
        gpus = [gpus[k] for k in order]
        ts_clean = ",".join(shares[k] for k in order) if shares is not None else None
        clean.append({"gpus": gpus, "n_slots": n, "tensor_split": ts_clean})
    warnings = small_gpu_warnings(clean, small_gpu)
    return clean, warnings


def instance_specs(profile: dict) -> list:
    """Servers to launch. Call after core.config.apply_runtime(profile) so gpu_devices is the active runtime's list."""
    if main_mode(profile) != "multi":
        return [{"index": 0, "port": LLAMA_SERVER_PORT, "gpus": list(profile.get("gpu_devices") or []),
                 "n_slots": int(profile.get("n_slots") or 1), "tensor_split": None, "single": True}]
    present = {int(d) for d in (profile.get("gpu_devices") or [])}
    specs = []
    for i, item in enumerate(saved_instances(profile)):
        # the port follows the list position, so a skipped server keeps its number and port
        if not isinstance(item, dict):
            continue
        gpus_in = [g for g in (item.get("gpus") or []) if isinstance(g, int) and not isinstance(g, bool)]
        keep = [k for k, g in enumerate(gpus_in) if g in present]
        if not keep:
            continue          # the machine changed since this was saved; skip rather than fail the whole main model
        ts = item.get("tensor_split")
        shares = str(ts).split(",") if ts and len(gpus_in) > 1 and len(keep) == len(gpus_in) else None
        if shares is not None and len(shares) != len(gpus_in):
            shares = None     # a split that no longer lines up with its GPUs is dropped, not guessed at
        # sort GPUs and shares together, the same way validate_instances does
        pairs = sorted((gpus_in[k], shares[k].strip() if shares else None) for k in keep)
        gpus = sorted({g for g, _ in pairs})
        ts_out = ",".join(s for _, s in pairs) if shares is not None and len(gpus) == len(pairs) and len(gpus) > 1 else None
        specs.append({"index": i, "port": instance_port(i), "gpus": gpus,
                      "n_slots": int(item.get("n_slots") or 1), "tensor_split": ts_out, "single": False})
    return specs
