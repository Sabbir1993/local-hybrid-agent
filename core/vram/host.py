"""System-memory telemetry for llama.cpp CPU/DRAM offload preflight."""

import psutil


class HostMemoryQueryError(RuntimeError):
    """Available system memory could not be measured safely."""


def host_memory_snapshot() -> dict[str, int]:
    try:
        memory = psutil.virtual_memory()
        swap = psutil.swap_memory()
        return {
            "total_b": int(memory.total),
            "available_b": int(memory.available),
            "swap_total_b": int(swap.total),
            "swap_used_b": int(swap.used),
        }
    except Exception as exc:
        raise HostMemoryQueryError(f"could not read system memory: {exc}") from exc
