import json
import re
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


# ---------------------------------------------------------------------------
# Model discovery and companion files (MTP draft, vision projector)
#
# Layout: one folder per model under the models directory,
#   Models/<model name>/<model>.gguf  (+ mtp-*.gguf, *mmproj*.gguf, model.json)
# Companions are only looked up inside the model's own folder and are attached
# only when their GGUF header fits the model (hidden size / architecture), so
# a 27B MTP draft or projector can never be paired with a 9B. Flat files in the
# models root still work, with exact-name companions only. "orchestrator/"
# holds the helper models (config/app.json small_models) and is not listed.
# ---------------------------------------------------------------------------

HELPER_DIRS = {"orchestrator"}
MANIFEST = "model.json"
_SHARD_RE = re.compile(r"-(\d{5})-of-(\d{5})\.gguf$", re.I)
_extra_roots: list = []


def register_models_root(path) -> None:
    """Extra models root (server_manager --models-dir)."""
    p = Path(path)
    if p not in _extra_roots:
        _extra_roots.append(p)


def models_roots() -> list:
    roots = []
    for r in _extra_roots + [MODELS_DIR]:
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


def discover_models() -> list:
    """Main-model GGUFs: Models/<folder>/*.gguf plus legacy flat files.
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
            entries += [(f, d.name) for f in sorted(d.glob("*.gguf"))]
        for f, folder in entries:
            if is_companion_file(f) or _is_later_shard(f):
                continue
            key = str(f.resolve())
            if key in seen:
                continue
            seen.add(key)
            out.append((f, folder))
    return out


def _manifest(folder: Path) -> dict:
    mf = folder / MANIFEST
    try:
        if mf.exists():
            d = json.loads(mf.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
    except Exception as e:
        print(f"[server_manager] {mf} unreadable: {e}", file=sys.stderr)
    return {}


def _check_mmproj(model_info: dict, cand: Path) -> Optional[str]:
    """None when the projector fits the model, else the reason it does not."""
    from .vram import read_gguf_scalars
    meta = read_gguf_scalars(cand, ("general.architecture", "clip.vision.projection_dim"))
    if meta.get("general.architecture") not in (None, "clip"):
        return f"{cand.name} is not a vision projector"
    want, got = model_info.get("n_embd"), meta.get("clip.vision.projection_dim")
    if want and got and int(want) != int(got):
        return f"{cand.name} is for a {got}-wide model (this model is {want})"
    return None


def _check_mtp(model_info: dict, cand: Path) -> Optional[str]:
    """None when the MTP draft belongs to this model, else the reason."""
    from .vram import parse_gguf_info, read_gguf_scalars
    info = parse_gguf_info(cand) or {}
    arch = model_info.get("arch")
    if arch and info.get("arch") and info["arch"] != arch:
        return f"{cand.name} is a {info['arch']} draft (this model is {arch})"
    want, got = model_info.get("n_embd"), info.get("n_embd")
    if want and got and int(want) != int(got):
        return f"{cand.name} is for a {got}-wide model (this model is {want})"
    a = info.get("arch") or arch
    if a:
        key = f"{a}.nextn_predict_layers"
        nextn = read_gguf_scalars(cand, (key,)).get(key)
        if nextn is not None and int(nextn) <= 0:
            return f"{cand.name} has no MTP layers"
    return None


def companions(model_path) -> dict:
    """{"mtp": Path|None, "mmproj": Path|None, "mtp_note": str, "mmproj_note": str}.

    Folder layout: candidates are the companion files in the model's folder;
    model.json {"mmproj": "<file>", "mtp": "<file>"} pins a choice (a file name
    inside that folder only; "" = none). Flat layout: exact names only
    (mtp-<stem>.gguf, mmproj-<stem>.gguf, <stem>-mmproj.gguf). Every candidate
    must pass the header check; the note explains a rejection."""
    from .vram import parse_gguf_info
    target = Path(model_path)
    res = {"mtp": None, "mmproj": None, "mtp_note": "", "mmproj_note": ""}
    if not target.exists():
        return res
    folder = target.parent
    if _is_flat(target):
        cands = {"mtp": [folder / f"mtp-{target.stem}.gguf"],
                 "mmproj": [folder / f"mmproj-{target.stem}.gguf",
                            folder / f"{target.stem}-mmproj.gguf"]}
    else:
        files = sorted(f for f in folder.glob("*.gguf") if f.is_file())
        cands = {"mtp": [f for f in files if f.name.lower().startswith("mtp-")],
                 "mmproj": [f for f in files if "mmproj" in f.name.lower()]}
        mf = _manifest(folder)
        for kind in ("mtp", "mmproj"):
            if kind not in mf:
                continue
            name = str(mf.get(kind) or "")
            if not name:
                cands[kind] = []                       # pinned: none
            elif Path(name).name == name and (folder / name).is_file():
                cands[kind] = [folder / name]
            else:
                res[f"{kind}_note"] = f"{MANIFEST}: {name!r} is not a file in this folder"
                cands[kind] = []
    cands = {k: [c for c in v if c.exists()] for k, v in cands.items()}
    if not cands["mtp"] and not cands["mmproj"]:
        return res
    info = parse_gguf_info(target) or {}
    stem = target.stem.lower()
    for kind, check in (("mtp", _check_mtp), ("mmproj", _check_mmproj)):
        # prefer a file named after this exact model, then alphabetical
        ordered = sorted(cands[kind], key=lambda c: (stem not in c.name.lower(), c.name.lower()))
        reasons = []
        for c in ordered:
            why = check(info, c)
            if why is None:
                res[kind] = c
                break
            reasons.append(why)
        if res[kind] is None and reasons:
            res[f"{kind}_note"] = "; ".join(reasons)
    return res


def find_mtp_draft(model_path: str) -> Optional[Path]:
    """MTP draft for a target GGUF (same folder, header-checked)."""
    return companions(model_path)["mtp"]


def find_mmproj(model_path: str) -> Optional[Path]:
    """Vision projector for a main GGUF (same folder, header-checked)."""
    return companions(model_path)["mmproj"]


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
    return prof
