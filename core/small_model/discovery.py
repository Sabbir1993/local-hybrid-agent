from pathlib import Path
from typing import Optional

from ..config import ACTIVE_RUNTIME


def whisper_search_dirs() -> list:
    """Where whisper-server(.exe) is looked for, first match wins."""
    out = []
    v = ACTIVE_RUNTIME.get("whisper_bin_dir")
    if v:
        out.append(Path(v))
    llama = Path(ACTIVE_RUNTIME["llama_bin_dir"])
    out += [llama.parent / "whisper-vulkan", llama.parent / "whisper", llama]
    return out


def sd_search_dirs() -> list:
    """Where sd-server(.exe) (stable-diffusion.cpp) is looked for."""
    out = []
    v = ACTIVE_RUNTIME.get("sd_bin_dir")
    if v:
        out.append(Path(v))
    llama = Path(ACTIVE_RUNTIME["llama_bin_dir"])
    out += [llama.parent / "sd-vulkan", llama.parent / "sd"]
    return out


def find_sd_server() -> Optional[Path]:
    for d in sd_search_dirs():
        for n in ("sd-server.exe", "sd-server"):
            p = d / n
            if p.is_file():
                return p
    return None


def find_whisper_server() -> Optional[Path]:
    for d in whisper_search_dirs():
        for n in ("whisper-server.exe", "whisper-server"):
            p = d / n
            if p.is_file():
                return p
    return None
