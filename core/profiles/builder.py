from pathlib import Path
from ..config import (
    CONFIG_DEFAULTS,
    CONFIG_TARGETS,
    MODEL_CONFIG_KEYS,
    apply_runtime,
)
from .companions import companions
from .config_store import load_model_configs


def build_dynamic_profile(p: Path) -> dict:
    """Profile dict for a raw GGUF picked from the models directory."""
    comp = companions(p)
    mtp, mmproj = comp["mtp"], comp["mmproj"]
    prof = {
        "name": p.stem,
        "description": f"Dynamic GGUF model ({p.name})",
        "model_type": "dense",
        "model_path": str(p),
        "mtp_draft_path": str(mtp) if mtp else None,
        "mmproj_path": str(mmproj) if mmproj else None,
        "mtp_note": comp["mtp_note"],
        "mmproj_note": comp["mmproj_note"],
    }
    for k, v in CONFIG_DEFAULTS.items():
        prof.setdefault(k, v)

    configs = load_model_configs()
    m_name = p.name.lower()
    m_stem = p.stem.lower()
    saved = configs.get(m_name) or configs.get(m_stem) or {}
    for k in MODEL_CONFIG_KEYS:
        if k in saved:
            prof[k] = saved[k]

    if prof.get("mtp_draft_path"):
        prof.setdefault("mtp_enabled", True)
    if prof.get("mmproj_path"):
        prof.setdefault("vision_capable", True)
    return apply_runtime(prof)
