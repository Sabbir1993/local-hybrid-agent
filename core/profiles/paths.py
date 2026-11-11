import re
from pathlib import Path
from .constants import (
    HELPER_DIR_NAME,
    MEDIA_DIR_NAMES,
    _SHARD_RE,
    _get_extra_roots,
    _get_media_dirs_cfg,
    _get_models_dir,
)


def register_models_root(path) -> None:
    """Extra models root (server_manager --models-dir)."""
    p = Path(path)
    extra = _get_extra_roots()
    if p not in extra:
        extra.append(p)


def models_roots() -> list:
    roots = []
    for r in _get_extra_roots() + [_get_models_dir()]:
        if r not in roots:
            roots.append(r)
    return roots


def in_models_dir(p) -> bool:
    """True when p is inside a models root (launch/preflight targets)."""
    try:
        rp = Path(p).resolve()
    except OSError:
        return False
    return any(rp.is_relative_to(r.resolve()) for r in models_roots())


def helper_root() -> Path:
    return _get_models_dir() / HELPER_DIR_NAME


def media_root(kind: str) -> Path:
    """Folder for image / video / voice model files: app.json media_dirs, else
    Models/orchestrator/<image-models|video-models|voice-models>."""
    cfg = _get_media_dirs_cfg()
    v = cfg.get(kind)
    if v:
        p = Path(v)
        return p if p.is_absolute() else _get_models_dir() / p
    return helper_root() / MEDIA_DIR_NAMES[kind]


def _inside(p, root: Path) -> bool:
    try:
        return Path(p).resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def in_helper_dir(p) -> bool:
    """True when p is inside Models/orchestrator (helper-lane model files)."""
    return _inside(p, helper_root())


def in_media_dir(p, kind: str) -> bool:
    """True when p is inside Models/orchestrator/<image-models|video-models|voice-models>."""
    return _inside(p, media_root(kind))


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(_get_models_dir())).replace("\\", "/")
    except ValueError:
        return str(p)


def _entry(p: Path) -> dict:
    from ..vram.gguf_parser import model_files, model_size_bytes
    try:
        size_gb = round(model_size_bytes(p) / 1e9, 2)      # all parts of a split model, so the fit hint is right
    except OSError:
        size_gb = None
    return {"path": _rel(p), "name": p.name, "size_gb": size_gb, "parts": len(model_files(p)) or 1}


def is_companion_file(p: Path) -> bool:
    n = p.name.lower()
    return n.startswith("mtp-") or "mmproj" in n


def _is_later_shard(p: Path) -> bool:
    m = _SHARD_RE.search(p.name)
    return bool(m) and int(m.group(1)) != 1


def _is_flat(target: Path) -> bool:
    """True when the GGUF sits directly in a models root (legacy layout)."""
    try:
        parent = target.parent.resolve()
    except OSError:
        return True
    return any(parent == r.resolve() for r in models_roots())


_PART_SUFFIX_RE = re.compile(r"-\d{5}-of-\d{5}$", re.I)


def model_display_name(name_or_path) -> str:
    """A model's name for display: file name without folders, ".gguf" and the "-00001-of-00002" part suffix of a split
    model (which is launched from its part-1 file). Same rule as cleanModelName in frontend/src/utils/models.ts."""
    base = str(name_or_path or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if base.lower().endswith(".gguf"):
        base = base[:-5]
    return _PART_SUFFIX_RE.sub("", base).strip()

