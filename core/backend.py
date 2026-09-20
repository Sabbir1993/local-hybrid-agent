"""Backend-specific naming for the llama.cpp binary in use (Vulkan or CUDA).

Everything else (tensor-split math, VRAM preflight, autotune sweep) is
already backend-agnostic; only the `-dev <prefix><N>` device string and the
GGML "visible devices" env var name differ between backends.
"""

DEVICE_PREFIX = {"vulkan": "Vulkan", "cuda": "CUDA"}
VISIBLE_DEVICES_ENV = {"vulkan": "GGML_VK_VISIBLE_DEVICES", "cuda": "CUDA_VISIBLE_DEVICES"}


def device_prefix(backend: str) -> str:
    return DEVICE_PREFIX.get(str(backend).lower(), DEVICE_PREFIX["vulkan"])


def visible_devices_env(backend: str) -> str:
    return VISIBLE_DEVICES_ENV.get(str(backend).lower(), VISIBLE_DEVICES_ENV["vulkan"])
