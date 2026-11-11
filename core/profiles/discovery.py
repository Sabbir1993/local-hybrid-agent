from pathlib import Path
from ..engines.vllm import is_hf_folder
from .constants import HELPER_DIRS, MEDIA_DIR_NAMES
from .paths import (
    _entry,
    _is_later_shard,
    helper_root,
    is_companion_file,
    media_root,
    models_roots,
)


def discover_helper_files(limit: int = 300) -> dict:
    """Model files the Settings wizard may pick for helper lanes.
    {"folder", "models", "mmproj", "whisper", "image", "video"}; paths are
    relative to the models dir (as config/app.json stores them). The image /
    video subfolders are listed apart so a big diffusion model never shows up
    as a text helper."""
    root = helper_root()
    media_dirs = {media_root(k).resolve() for k in MEDIA_DIR_NAMES}
    out = {"folder": str(root), "models": [], "mmproj": [], "whisper": [],
           "image": [], "video": []}
    if not root.is_dir():
        return out

    def _walk(d: Path, depth: int = 0):
        try:
            items = sorted(d.iterdir())
        except OSError:
            return
        for f in items:
            if f.name.startswith("."):
                continue
            if f.is_dir():
                if depth < 2 and f.resolve() not in media_dirs:
                    yield from _walk(f, depth + 1)
            elif f.is_file():
                yield f

    for f in _walk(root):
        if sum(len(v) for v in out.values() if isinstance(v, list)) >= limit:
            break
        n = f.name.lower()
        if n.endswith(".gguf"):
            if "mmproj" in n:
                out["mmproj"].append(_entry(f))
            elif not n.startswith("mtp-") and not _is_later_shard(f):
                e = _entry(f)
                e["display"] = f.stem
                out["models"].append(e)
    # a projector next to a model (same folder) is offered as its default
    for m in out["models"]:
        folder = str(Path(m["path"]).parent)
        mm = [x for x in out["mmproj"] if str(Path(x["path"]).parent) == folder]
        m["mmproj_path"] = mm[0]["path"] if len(mm) == 1 else None
    for kind, key in (("image_gen", "image"), ("video_gen", "video")):
        d = media_root(kind)
        if not d.is_dir():
            continue
        for f in sorted(d.rglob("*")):
            if len(out[key]) >= limit:
                break
            if f.is_file() and f.suffix.lower() in (".gguf", ".safetensors"):
                out[key].append(_entry(f))
    # whisper.cpp models (ggml .bin) come only from voice-models
    d = media_root("stt")
    if d.is_dir():
        for f in sorted(d.rglob("*.bin")):
            if len(out["whisper"]) >= limit:
                break
            if f.is_file():
                out["whisper"].append(_entry(f))
    return out


def discover_models() -> list:
    """Main models: Models/<folder>/*.gguf plus legacy flat files, and Hugging Face model folders (config.json +
    safetensors, for vLLM) at Models/<model>/ or Models/<folder>/<model>/.
    Returns [(path, folder_name_or_None), ...] without companions, later
    split shards or the helper-model folder."""
    out, seen = [], set()
    for root in models_roots():
        if not root.is_dir():
            continue
        entries = [(f, None) for f in sorted(root.glob("*.gguf"))]
        for d in sorted(x for x in root.iterdir() if x.is_dir()):
            if d.name.lower() in HELPER_DIRS or d.name.startswith("."):
                continue
            # a Hugging Face model folder (vLLM), directly in the root or one level down
            if is_hf_folder(d):
                entries.append((d, None))
                continue
            entries += [(f, d.name) for f in sorted(d.glob("*.gguf"))]
            entries += [(sub, d.name) for sub in sorted(x for x in d.iterdir() if x.is_dir() and not x.name.startswith("."))
                        if is_hf_folder(sub)]
        for f, folder in entries:
            if is_companion_file(f) or _is_later_shard(f):
                continue
            key = str(f.resolve())
            if key in seen:
                continue
            seen.add(key)
            out.append((f, folder))
    return out
