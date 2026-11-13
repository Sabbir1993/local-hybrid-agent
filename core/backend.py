"""llama.cpp backend registry and runtime-scoped child environments.

The application selects exactly one native llama.cpp runtime before startup.
Backend lookup therefore fails closed: silently treating an unknown runtime as
Vulkan can launch the wrong binary with a misleading device list.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


class BackendConfigError(ValueError):
    """A configured llama.cpp backend or runtime environment is invalid."""


@dataclass(frozen=True)
class BackendSpec:
    name: str
    device_prefixes: tuple[str, ...]
    visible_devices_env: str | None
    runtime_env_keys: frozenset[str] = frozenset()
    multi_gpu: bool = True
    tensor_split: bool = True
    partial_offload: bool = True

    @property
    def device_prefix(self) -> str:
        return self.device_prefixes[0]


_SYCL_ENV_KEYS = frozenset({
    "ONEAPI_DEVICE_SELECTOR",
    "ZE_AFFINITY_MASK",
    "SYCL_CACHE_PERSISTENT",
    "SYCL_CACHE_DIR",
    "UR_L0_USE_IMMEDIATE_COMMANDLISTS",
    # Older oneAPI releases use the PI spelling.
    "SYCL_PI_LEVEL_ZERO_USE_IMMEDIATE_COMMANDLISTS",
})

BACKENDS: dict[str, BackendSpec] = {
    "vulkan": BackendSpec(
        name="vulkan",
        device_prefixes=("Vulkan",),
        visible_devices_env="GGML_VK_VISIBLE_DEVICES",
    ),
    "cuda": BackendSpec(
        name="cuda",
        device_prefixes=("CUDA",),
        visible_devices_env="CUDA_VISIBLE_DEVICES",
    ),
    "sycl": BackendSpec(
        name="sycl",
        device_prefixes=("SYCL",),
        # SYCL device selection is passed to llama.cpp with exact -dev IDs.
        # Do not guess a ZE_AFFINITY_MASK mapping from llama.cpp indices.
        visible_devices_env=None,
        runtime_env_keys=_SYCL_ENV_KEYS,
    ),
}

# Compatibility exports used by device parsing and tests. Values are derived
# from the registry so they cannot drift from backend lookup.
DEVICE_PREFIX = {name: spec.device_prefix for name, spec in BACKENDS.items()}
VISIBLE_DEVICES_ENV = {name: spec.visible_devices_env for name, spec in BACKENDS.items()}

_ALL_RUNTIME_ENV_KEYS = frozenset(
    key
    for spec in BACKENDS.values()
    for key in (set(spec.runtime_env_keys) |
                ({spec.visible_devices_env} if spec.visible_devices_env else set()))
)


def get_backend(backend: object) -> BackendSpec:
    key = str(backend or "").strip().lower()
    try:
        return BACKENDS[key]
    except KeyError as exc:
        supported = ", ".join(sorted(BACKENDS))
        raise BackendConfigError(
            f"unknown llama.cpp backend {backend!r}; expected one of: {supported}"
        ) from exc


def device_prefix(backend: object) -> str:
    return get_backend(backend).device_prefix


def visible_devices_env(backend: object) -> str | None:
    return get_backend(backend).visible_devices_env


def validate_runtime_env(backend: object, values: object) -> dict[str, str]:
    """Validate administrator-supplied environment overrides for one runtime.

    Library lookup paths belong in the service definition, not app.json. The
    allowlist keeps config/API edits from becoming arbitrary process injection.
    """
    spec = get_backend(backend)
    if values in (None, {}):
        return {}
    if not isinstance(values, Mapping):
        raise BackendConfigError(
            f"runtimes.*.env for backend '{spec.name}' must be an object"
        )
    clean: dict[str, str] = {}
    for raw_key, raw_value in values.items():
        key = str(raw_key)
        if key not in spec.runtime_env_keys:
            allowed = ", ".join(sorted(spec.runtime_env_keys)) or "(none)"
            raise BackendConfigError(
                f"environment key {key!r} is not allowed for backend "
                f"'{spec.name}'; allowed: {allowed}"
            )
        if not isinstance(raw_value, str):
            raise BackendConfigError(
                f"environment value for {key!r} must be a string"
            )
        if "\x00" in raw_value:
            raise BackendConfigError(
                f"environment value for {key!r} contains a NUL byte"
            )
        clean[key] = raw_value
    return clean


def build_child_env(
    backend: object,
    *,
    runtime_env: object = None,
    device_indices: list[int] | tuple[int, ...] | None = None,
    base_env: Mapping[str, str] | None = None,
    set_visibility: bool = True,
) -> dict[str, str]:
    """Return an isolated environment for a child of the active runtime.

    Selector variables belonging to inactive backends are removed. Active
    oneAPI variables inherited from a service wrapper are preserved, while
    validated app.json values override them. Vulkan/CUDA device visibility is
    derived from the selected numeric devices. SYCL uses exact llama.cpp
    `-dev` identifiers and deliberately does not synthesize an affinity mask.
    """
    spec = get_backend(backend)
    env = dict(os.environ if base_env is None else base_env)
    active_keys = set(spec.runtime_env_keys)
    if spec.visible_devices_env:
        active_keys.add(spec.visible_devices_env)
    for key in _ALL_RUNTIME_ENV_KEYS - active_keys:
        env.pop(key, None)
    # Exact llama.cpp -dev IDs are safer than visibility variables because
    # CUDA-style filters may renumber devices inside the child process.
    if spec.visible_devices_env:
        env.pop(spec.visible_devices_env, None)
    env.update(validate_runtime_env(spec.name, runtime_env))
    if set_visibility and device_indices and spec.visible_devices_env:
        env[spec.visible_devices_env] = ",".join(str(int(i)) for i in device_indices)
    return env
