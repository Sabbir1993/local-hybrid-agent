from fastapi import APIRouter
from core.backend import device_prefix
from core.config import CONFIG_DEFAULTS
from core.state import state


def _gpu_device_label(gpu_devices: list) -> str:
    """UI device badge for a set of local GPU indices, e.g. '2x GPU (Vulkan)'.
    Vendor-neutral: works for any backend/GPU count instead of assuming
    'Intel Arc A770'."""
    backend = (state.profile or {}).get("backend", CONFIG_DEFAULTS["backend"])
    prefix = device_prefix(backend)
    n = len(gpu_devices) if gpu_devices else 1
    return f"{n}x GPU ({prefix})" if n > 1 else f"GPU ({prefix})"


def _main_device_label() -> str:
    gpu_devices = (state.profile or {}).get("gpu_devices") or CONFIG_DEFAULTS["gpu_devices"]
    return _gpu_device_label(gpu_devices)


def _executor_device_label(gpu: "int | None" = None) -> str:
    backend = (state.profile or {}).get("backend", CONFIG_DEFAULTS["backend"])
    prefix = device_prefix(backend)
    return f"GPU #{gpu} ({prefix}{gpu})" if gpu is not None else f"GPU ({prefix})"


router = APIRouter(tags=["agent"])
