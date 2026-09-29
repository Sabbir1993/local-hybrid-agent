import json
import sys
from pathlib import Path
from typing import Optional
from .constants import MANIFEST
from .paths import _is_flat


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
    from ..vram import read_gguf_scalars
    meta = read_gguf_scalars(cand, ("general.architecture", "clip.vision.projection_dim"))
    if meta.get("general.architecture") not in (None, "clip"):
        return f"{cand.name} is not a vision projector"
    want, got = model_info.get("n_embd"), meta.get("clip.vision.projection_dim")
    if want and got and int(want) != int(got):
        return f"{cand.name} is for a {got}-wide model (this model is {want})"
    return None


def _check_mtp(model_info: dict, cand: Path) -> Optional[str]:
    """None when the MTP draft belongs to this model, else the reason."""
    from ..vram import parse_gguf_info, read_gguf_scalars
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
    from ..vram import parse_gguf_info
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
