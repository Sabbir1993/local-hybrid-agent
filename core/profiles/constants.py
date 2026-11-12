import json
import re
import sys
import time
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


_dup_cache: dict = {}


def _same_name_elsewhere(p: Path) -> bool:
    """True when another model folder called p.name sits in a different place of the models roots (directly in a root or one
    level down, where model folders live). Kept for a few seconds: the key is asked for on every config request."""
    roots = tuple(str(r) for r in [*_get_extra_roots(), _get_models_dir()])
    ck = (p.name.lower(), str(p), roots)
    hit = _dup_cache.get(ck)
    if hit and time.monotonic() - hit[0] < 5:
        return hit[1]
    me, found = p.resolve(), False
    try:
        for r in roots:
            base = Path(r)
            cands = [base / p.name] + [d / p.name for d in base.iterdir() if d.is_dir()]
            for c in cands:
                if c.is_dir() and c.resolve() != me and c.name.lower() == p.name.lower():
                    found = True
                    break
            if found:
                break
    except OSError:
        pass
    if len(_dup_cache) > 64:
        _dup_cache.clear()
    _dup_cache[ck] = (time.monotonic(), found)
    return found


def _model_key(identifier: Union[str, Path]) -> str:
    """Normalize a model path or name into a canonical model-name key. Two model folders with the same name in different
    places get "<name>@<parent folder>" so their saved settings stay apart; every other key is just the name."""
    s = str(identifier).strip()
    p = Path(s)
    name = p.name.lower()
    try:
        if p.is_absolute() and p.parent.name and p.is_dir() and _same_name_elsewhere(p):
            return f"{name}@{p.parent.name.lower()}"
    except OSError:
        pass
    return name


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
