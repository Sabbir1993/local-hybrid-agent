import json
import re
import sys
from pathlib import Path
from typing import Union
from ..config import BASE_DIR, CONFIG_FILE

HELPER_DIRS = {"orchestrator"}
MANIFEST = "model.json"
_SHARD_RE = re.compile(r"-(\d{5})-of-(\d{5})\.gguf$", re.I)
_extra_roots: list = []

HELPER_DIR_NAME = "orchestrator"
MEDIA_DIR_NAMES = {"image_gen": "image-models", "video_gen": "video-models", "stt": "voice-models"}
# config/app.json "media_dirs" key -> kind (absolute, or relative to models_dir). Read at start.
MEDIA_DIR_KEYS = {"image_models": "image_gen", "video_models": "video_gen", "voice_models": "stt"}


def _get_config_file() -> Path:
    return getattr(sys.modules.get("core.profiles"), "CONFIG_FILE", CONFIG_FILE)


def _load_models_dir_config() -> Path:
    """Models directory: config/app.json 'models_dir' > E:/AI/Models > ./models"""
    cfg = _get_config_file()
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


def _get_models_dir() -> Path:
    return getattr(sys.modules.get("core.profiles"), "MODELS_DIR", MODELS_DIR)


def _get_extra_roots() -> list:
    return getattr(sys.modules.get("core.profiles"), "_extra_roots", _extra_roots)


def _model_key(identifier: Union[str, Path]) -> str:
    """Normalize a model path or name into a canonical model-name key."""
    s = str(identifier).strip()
    return Path(s).name.lower()


def _load_media_dirs_config() -> dict:
    cfg = _get_config_file()
    try:
        d = json.loads(cfg.read_text()).get("media_dirs") if cfg.exists() else None
    except Exception:
        return {}
    if not isinstance(d, dict):
        return {}
    return {MEDIA_DIR_KEYS[k]: str(v).strip() for k, v in d.items()
            if k in MEDIA_DIR_KEYS and isinstance(v, str) and v.strip()}


MEDIA_DIRS_CFG = _load_media_dirs_config()


def _get_media_dirs_cfg() -> dict:
    return getattr(sys.modules.get("core.profiles"), "MEDIA_DIRS_CFG", MEDIA_DIRS_CFG)
