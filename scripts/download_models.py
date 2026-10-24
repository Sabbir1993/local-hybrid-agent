#!/usr/bin/env python3
"""Download the models and helper programs the app needs - Windows, Linux and macOS.

Standard library only, so it runs before (or without) the venv:

    python scripts/download_models.py                  # list the groups, download nothing
    python scripts/download_models.py --all            # every model group (~25 GB)
    python scripts/download_models.py routing vision embedder voice image
    python scripts/download_models.py --binaries --backend vulkan     # llama.cpp + stable-diffusion.cpp programs
    python scripts/download_models.py --all --write-config            # also point config/app.json at what was fetched

Re-running is safe: files that already exist are skipped, partial downloads resume.
Folders follow config/app.json (models_dir, media_dirs); --models-dir overrides.
Nothing here is secret: every source is a public Hugging Face / GitHub URL.
"""
import argparse
import json
import os
import platform
import shutil
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "app.json"
HF = "https://huggingface.co/{repo}/resolve/main/{file}"

# group -> (what it is, [(url, destination under its folder, approx MB)])
# "orch" = <models_dir>/orchestrator, "voice"/"image" = media_dirs.voice_models / image_models.
GROUPS = {
    "routing": ("Executor / tool-calling model (small_models.executor, also drives tool routing)", "orch", [
        (HF.format(repo="ornith-ai/Ornith-1.5-9B-GGUF", file="Ornith-1.5-9B-Q4_K_M.gguf"),
         "Ornith-1.5-9B-Q4_K_M.gguf", 5700),
    ]),
    "vision": ("Image reader (small_models.vision) with its projector", "orch", [
        (HF.format(repo="ggml-org/Qwen2.5-VL-3B-Instruct-GGUF", file="Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf"),
         "Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf", 1900),
        # saved under the name config/app.json expects
        (HF.format(repo="ggml-org/Qwen2.5-VL-3B-Instruct-GGUF", file="mmproj-Qwen2.5-VL-3B-Instruct-f16.gguf"),
         "mmproj-model-f16.gguf", 1300),
    ]),
    "embedder": ("Embeddings for memory and the knowledge base (small_models.embedder)", "orch", [
        (HF.format(repo="nomic-ai/nomic-embed-text-v1.5-GGUF", file="nomic-embed-text-v1.5.Q8_0.gguf"),
         "nomic-embed-text-v1.5-Q8_0.gguf", 140),
    ]),
    "voice": ("Speech to text (whisper.cpp) + spoken replies (sherpa-onnx, English and Bangla)", "voice", [
        (HF.format(repo="ggerganov/whisper.cpp", file="ggml-large-v3-turbo-q5_0.bin"),
         "ggml-large-v3-turbo-q5_0.bin", 570),
        (HF.format(repo="ggerganov/whisper.cpp", file="ggml-large-v3.bin"),
         "ggml-large-v3.bin", 3100),
        ("https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/vits-piper-en_US-lessac-medium.tar.bz2",
         "tts/", 65),
        ("https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/vits-coqui-bn-custom_female.tar.bz2",
         "tts/", 100),
    ]),
    "image": ("Local image maker (z-image-turbo for stable-diffusion.cpp)", "image", [
        (HF.format(repo="leejet/Z-Image-Turbo-GGUF", file="z_image_turbo-Q4_K.gguf"),
         "z_image_turbo-Q4_K.gguf", 4000),
        (HF.format(repo="unsloth/Qwen3-4B-Instruct-2507-GGUF", file="Qwen3-4B-Instruct-2507-Q4_K_M.gguf"),
         "Qwen3-4B-Instruct-2507-Q4_K_M.gguf", 2500),
        (HF.format(repo="Comfy-Org/z_image_turbo", file="split_files/vae/ae.safetensors"),
         "ae.safetensors", 330),
    ]),
}
# CPU routers: fetched with huggingface_hub (installed by requirements.txt) into the Hugging Face cache
HF_REPOS = {"routing": ["convaiinnovations/laya"]}   # router.engine = "laya"; "cactus_needle" fetches Needle on first use


def _app_config() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _dirs(args) -> dict:
    cfg = _app_config()
    models = Path(args.models_dir or cfg.get("models_dir") or ROOT / "models")
    orch = models / "orchestrator"
    media = cfg.get("media_dirs") or {}

    def pick(key, default):
        # a --models-dir override moves the media folders too, otherwise honour the config
        v = None if args.models_dir else media.get(key)
        p = Path(v) if v else default
        return p if p.is_absolute() else models / p

    return {"models": models, "orch": orch,
            "voice": pick("voice_models", orch / "voice-models"),
            "image": pick("image_models", orch / "image-models")}


def _download(url: str, dest: Path, mb: int, dry: bool) -> bool:
    if dest.exists():
        print(f"  skip (exists)  {dest.name}")
        return True
    print(f"  fetch          {dest.name}  (~{mb} MB)")
    if dry:
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    req = urllib.request.Request(url, headers={"User-Agent": "a770-setup", **({"Range": f"bytes={have}-"} if have else {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            if have and r.status != 206:      # server ignored Range: start over
                have = 0
            total = int(r.headers.get("Content-Length") or 0) + have
            done = have
            with open(part, "ab" if have else "wb") as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
                    done += len(chunk)
                    if total and (done >> 20) % 5 == 0:
                        print(f"\r    {done >> 20} / {total >> 20} MB", end="", flush=True)
        print()
    except Exception as e:
        print(f"\n  FAILED {dest.name}: {e}  (re-run to resume)", file=sys.stderr)
        return False
    os.replace(part, dest)
    return True


def _safe_extract(archive: Path, into: Path) -> None:
    into.mkdir(parents=True, exist_ok=True)
    root = into.resolve()
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as z:
            for n in z.namelist():
                if not (root / n).resolve().is_relative_to(root):
                    raise ValueError(f"unsafe path in archive: {n}")
            z.extractall(into)
        return
    with tarfile.open(archive) as t:
        for m in t.getmembers():
            if not (root / m.name).resolve().is_relative_to(root) or m.islnk() or m.issym():
                raise ValueError(f"unsafe entry in archive: {m.name}")
        t.extractall(into)


def fetch_group(name: str, dirs: dict, dry: bool) -> bool:
    desc, where, items = GROUPS[name]
    base = dirs[where]
    print(f"\n[{name}] {desc}\n  -> {base}")
    ok = True
    for url, rel, mb in items:
        if rel.endswith("/"):                       # an archive that unpacks into <base>/<rel>
            archive = Path(url.rsplit("/", 1)[1])
            target = base / rel / archive.name.split(".tar")[0]
            if target.is_dir():
                print(f"  skip (exists)  {target.name}/")
                continue
            tmp = base / rel / archive.name
            if _download(url, tmp, mb, dry) and not dry:
                _safe_extract(tmp, base / rel)
                tmp.unlink()
            elif not dry:
                ok = False
        else:
            ok &= _download(url, base / rel, mb, dry)
    for repo in HF_REPOS.get(name, []):
        print(f"  router model   {repo}  (Hugging Face cache)")
        if dry:
            continue
        try:
            from huggingface_hub import snapshot_download
            snapshot_download(repo_id=repo)
        except ImportError:
            print("    huggingface_hub not installed yet - run the setup script first; the router fetches it on first use anyway")
        except Exception as e:
            print(f"    could not fetch {repo}: {e}  (the router retries on first use)", file=sys.stderr)
    return ok


# ---- helper programs ---------------------------------------------------------------------------
def _release_assets(repo: str) -> list:
    """Assets of the newest release that has any (some repos tag source-only releases)."""
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/releases?per_page=10",
                                 headers={"User-Agent": "a770-setup", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        for rel in json.load(r):
            if rel.get("assets"):
                return rel["assets"]
    return []


def _os_key() -> str:
    return {"win32": "win", "darwin": "mac"}.get(sys.platform, "linux")


def fetch_binaries(backend: str, dest: Path, dry: bool) -> dict:
    """llama.cpp and stable-diffusion.cpp release builds. whisper.cpp ships no GPU build: see SETUP docs."""
    osk, arm = _os_key(), platform.machine().lower() in ("arm64", "aarch64")
    want = {   # program -> (repo, folder, substrings an asset name must contain, ... must not)
        "llama.cpp": ("ggml-org/llama.cpp", "llama-" + backend, {
            "win": ["bin-win-" + ("cuda-12" if backend == "cuda" else backend), "x64"],
            "linux": ["bin-ubuntu-" + ("cuda-12" if backend == "cuda" else backend), "x64"],
            "mac": ["bin-macos-" + ("arm64" if arm else "x64")]}[osk], ["cudart"]),
        "stable-diffusion.cpp": ("leejet/stable-diffusion.cpp", "sd-" + backend, {
            "win": ["bin-win-" + ("cuda12" if backend == "cuda" else backend)],
            "linux": ["Ubuntu", "x86_64"] + (["vulkan"] if backend == "vulkan" else []),
            "mac": ["macOS", "arm64"]}[osk], ["rocm"] if backend != "rocm" else []),
    }
    out = {}
    for prog, (repo, folder, must, never) in want.items():
        try:
            assets = _release_assets(repo)
        except Exception as e:
            print(f"\n[{prog}] could not read the release list ({e}); download by hand from https://github.com/{repo}/releases")
            continue
        hits = [a for a in assets if all(s in a["name"] for s in must) and not any(s in a["name"] for s in never)
                and a["name"].endswith((".zip", ".tar.gz"))]
        print(f"\n[{prog}] {backend} build for {osk} -> {dest / folder}")
        if not hits:
            print(f"  no matching release asset (looked for {must}); pick one by hand from https://github.com/{repo}/releases")
            continue
        a = hits[0]
        target = dest / folder
        if target.is_dir() and any(target.rglob("*server*")):
            print(f"  skip (exists)  {target}")
            out[prog] = target
            continue
        archive = dest / a["name"]
        if _download(a["browser_download_url"], archive, a["size"] >> 20, dry) and not dry:
            _safe_extract(archive, target)
            archive.unlink()
            # a single top-level folder inside the archive: hoist it so llama-server sits directly in <target>
            kids = list(target.iterdir())
            if len(kids) == 1 and kids[0].is_dir():
                for k in kids[0].iterdir():
                    shutil.move(str(k), target / k.name)
                kids[0].rmdir()
            for exe in target.rglob("*"):
                if exe.is_file() and exe.suffix == "" and ("server" in exe.name or "bench" in exe.name or exe.name.endswith("-cli")):
                    exe.chmod(exe.stat().st_mode | 0o111)
            out[prog] = target
    return out


def write_config(dirs: dict, binaries: dict, backend: str) -> None:
    """Point config/app.json at what was downloaded (atomic write, keeps every other key)."""
    cfg = _app_config()
    if not cfg:
        print("config/app.json not readable - not changed", file=sys.stderr)
        return
    cfg["models_dir"] = dirs["models"].as_posix()
    cfg["media_dirs"] = {"voice_models": dirs["voice"].as_posix(), "image_models": dirs["image"].as_posix(),
                         "video_models": (dirs["orch"] / "video-models").as_posix()}
    runtimes = cfg.setdefault("runtimes", {})
    if backend == "metal" and "metal" not in runtimes:     # macOS: one unified-memory GPU
        runtimes["metal"] = {"backend": "metal", "gpu_devices": [0], "small_model_gpu": 0}
    preset = runtimes.get(backend)
    if preset is not None:
        if "llama.cpp" in binaries:
            preset["llama_bin_dir"] = binaries["llama.cpp"].as_posix()
        if "stable-diffusion.cpp" in binaries:
            preset["sd_bin_dir"] = binaries["stable-diffusion.cpp"].as_posix()
        cfg["runtime"] = backend
    tmp = CONFIG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, CONFIG)
    print(f"\nconfig/app.json updated (models_dir, media_dirs{', ' + backend + ' runtime' if preset is not None else ''}).")
    print("GPU device indices (runtimes.<name>.gpu_devices) are machine specific: set them after running the list-devices script.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("groups", nargs="*", help="; ".join(GROUPS))
    ap.add_argument("--all", action="store_true", help="every model group")
    ap.add_argument("--binaries", action="store_true", help="also fetch llama.cpp + stable-diffusion.cpp builds")
    ap.add_argument("--backend", default="vulkan", choices=["vulkan", "cuda", "cpu", "metal"], help="GPU build for --binaries (macOS always uses Metal)")
    ap.add_argument("--bin-dir", help="where the programs go (default: next to the repo's parent folder)")
    ap.add_argument("--models-dir", help="override models_dir from config/app.json")
    ap.add_argument("--write-config", action="store_true", help="update config/app.json with these paths")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if sys.platform == "darwin":
        args.backend = "metal"        # llama.cpp's macOS build has Metal built in

    bad = [g for g in args.groups if g not in GROUPS]
    if bad:
        ap.error(f"unknown group {bad}; choose from {', '.join(GROUPS)}")
    chosen = list(GROUPS) if args.all else args.groups
    dirs = _dirs(args)
    if not chosen and not args.binaries:
        print("Model groups (name, size, what):")
        for n, (desc, _w, items) in GROUPS.items():
            print(f"  {n:9} ~{sum(i[2] for i in items) / 1024:4.1f} GB  {desc}")
        print(f"\nModels go to {dirs['models']}. Pick groups, or use --all / --binaries (see --help).")
        return 0
    ok = all([fetch_group(g, dirs, args.dry_run) for g in chosen])
    binaries = {}
    if args.binaries:
        bindir = Path(args.bin_dir) if args.bin_dir else ROOT.parent
        binaries = fetch_binaries(args.backend, bindir, args.dry_run)
    if args.write_config and not args.dry_run:
        write_config(dirs, binaries, args.backend)
    print("\nDone." if ok else "\nFinished with failures - re-run to resume.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
