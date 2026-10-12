"""Mixture-of-experts offload: keep the small hot weights on the GPU, put the experts in RAM.

A dense model can be split by layer (`-ngl N`): the layers past N run on the CPU. For a MoE model
that is the wrong cut. Its weights are ~90% routed experts, and a layer split sends the router
output across the GPU/CPU boundary mid-graph, which on the Vulkan build crashes the server
(`GGML_ASSERT(id >= 0 && id < n_expert)`). The cut llama.cpp supports for this is `--n-cpu-moe N`:
every layer stays on the GPU (attention, router, shared expert, KV cache) and only the expert
tensors of the first N layers live in RAM.

An explicit `n_gpu_layers` below the model's layer count is always launched as written (`-ngl 30`).
With full offload and no `tuned.n_cpu_moe`, the preflight picks the smallest N that fits the free
VRAM and launches `-ngl 999 --n-cpu-moe N`, so a MoE model loads without any hand tuning.
"""

from .gguf_parser import expert_layer_bytes

# Share of a MoE file that is expert tensors, used only when the tensor table cannot be read.
_FALLBACK_EXPERT_SHARE = 0.6

FULL_OFFLOAD = 999


def is_moe(profile: dict, info: dict | None) -> bool:
    if profile.get("model_type") == "moe":
        return True
    return bool(info and (info.get("n_expert") or 0) > 0)


def _int_or_none(v):
    try:
        return None if v is None else max(0, int(v))
    except (TypeError, ValueError):
        return None


def resolve_offload(profile: dict, info: dict | None, requested_ngl: int,
                    explicit: int | None, auto: int | None) -> tuple[int, int]:
    """(ngl to launch with, n_cpu_moe) for a profile.

    `explicit` is tuned.n_cpu_moe, `auto` is the value the preflight chose from free VRAM.
    Dense models are returned unchanged with n_cpu_moe 0.
    """
    if not is_moe(profile, info):
        return requested_ngl, 0
    n_layer = int((info or {}).get("n_layer") or 0)
    units = n_layer + 1
    explicit = _int_or_none(explicit)
    if explicit is not None:
        return requested_ngl, explicit
    if n_layer and 0 < requested_ngl < units:
        # the user chose a layer count: launch exactly that
        return requested_ngl, 0
    auto = _int_or_none(auto)
    if auto is not None:
        return FULL_OFFLOAD, auto
    return requested_ngl, 0


def expert_bytes_offloaded(model_path, info: dict, weights_b: int, n_cpu_moe: int) -> int:
    """Bytes of expert weights the first `n_cpu_moe` blocks keep in RAM."""
    n_layer = int(info.get("n_layer") or 0)
    if n_cpu_moe <= 0 or n_layer <= 0:
        return 0
    n = min(n_cpu_moe, n_layer)
    per_layer = expert_layer_bytes(model_path)
    if per_layer:
        return sum(per_layer.get(i, 0) for i in range(n))
    return int(weights_b * _FALLBACK_EXPERT_SHARE * n / n_layer)


def smallest_fitting(n_layer: int, fits) -> int | None:
    """Smallest n_cpu_moe in 0..n_layer for which fits(n) is true (fits is monotone), else None."""
    if n_layer <= 0 or not fits(n_layer):
        return None
    lo, hi = 0, n_layer
    while lo < hi:
        mid = (lo + hi) // 2
        if fits(mid):
            hi = mid
        else:
            lo = mid + 1
    return lo
