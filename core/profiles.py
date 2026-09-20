import json
import sys
from pathlib import Path
from typing import Optional, Union

from .config import (
    CONFIG_DEFAULTS,
    CONFIG_TARGETS,
    MODEL_CONFIG_KEYS,
    MODEL_CONFIGS_FILE,
    BASE_DIR,
    CONFIG_FILE,
)


def _load_models_dir_config() -> Path:
    """Models directory: config/app.json 'models_dir' > E:/AI/Models > ./models"""
    cfg = CONFIG_FILE
    candidates = []
    try:
        if cfg.exists():
            d = json.loads(cfg.read_text())
            v = d.get("models_dir")
            if v:
                candidates.append(Path(v))
    except Exception:
        pass
    candidates.extend([Path("E:/AI/Models"), BASE_DIR / "models"])
    for c in candidates:
        if c.is_dir():
            return c
    return candidates[-1]


MODELS_DIR = _load_models_dir_config()


def _model_key(identifier: Union[str, Path]) -> str:
    """Normalize a model path or name into a canonical model-name key."""
    s = str(identifier).strip()
    return Path(s).name.lower()


def load_model_configs() -> dict:
    store: dict = {}
    migrated = False
    try:
        if MODEL_CONFIGS_FILE.exists():
            raw = json.loads(MODEL_CONFIGS_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if "\\" in k or "/" in k or ":" in k:
                        norm_key = _model_key(k)
                        store[norm_key] = v
                        migrated = True
                    else:
                        store[k.lower()] = v
    except Exception as e:
        print(f"[server_manager] model_configs.json unreadable: {e}", file=sys.stderr)

    if migrated:
        try:
            MODEL_CONFIGS_FILE.write_text(json.dumps(store, indent=2), encoding="utf-8")
            print(f"[server_manager] migrated model_configs.json to model-name keys")
        except Exception as e:
            print(f"[server_manager] failed saving migrated model_configs.json: {e}", file=sys.stderr)

    return store


def save_model_config(model_identifier: str, cfg: dict) -> None:
    """Persist a subset of config for one model name in model_configs.json.

    n_gpu_layers / tensor_split live under the profile's "tuned" section while
    all other keys sit at the top level, so the tuned (effective) value wins.
    """
    store = load_model_configs()
    key = _model_key(model_identifier)
    tuned = cfg.get("tuned", {}) or {}
    out = {}
    for k in MODEL_CONFIG_KEYS:
        if k in tuned:
            out[k] = tuned[k]
        elif k in cfg:
            out[k] = cfg[k]
    store[key] = out
    try:
        MODEL_CONFIGS_FILE.write_text(json.dumps(store, indent=2), encoding="utf-8")
        print(f"[server_manager] saved model config for [{key}] in model_configs.json")
    except Exception as e:
        print(f"[server_manager] model_configs.json write failed: {e}", file=sys.stderr)


def find_mtp_draft(model_path: str) -> Optional[Path]:
    """Locate an MTP draft model for a given target GGUF."""
    target = Path(model_path)
    if not target.exists():
        return None
    search_dirs = {target.parent, MODELS_DIR}
    # exact sibling first
    for d in search_dirs:
        exact = d / f"mtp-{target.stem}.gguf"
        if exact.exists():
            return exact
    family = target.stem.split("-")[0].lower()
    candidates = []
    for d in search_dirs:
        if d.is_dir():
            candidates.extend(d.glob("mtp-*.gguf"))
    for c in sorted(candidates):
        if family in c.stem.lower():
            return c
    return None


def build_dynamic_profile(p: Path) -> dict:
    """Profile dict for a raw GGUF picked from the models directory."""
    prof = {
        "name": p.stem,
        "description": f"Dynamic GGUF model ({p.name})",
        "model_type": "dense",
        "model_path": str(p),
        "mtp_draft_path": str(find_mtp_draft(str(p))),
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

    prof["mtp_draft_path"] = str(find_mtp_draft(str(p)))
    if prof["mtp_draft_path"]:
        prof.setdefault("mtp_enabled", True)
    return prof
