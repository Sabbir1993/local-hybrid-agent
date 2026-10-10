"""project_overview: the layout of a project in ONE tool call.

Analysing a project used to be dozens of list_files / read_file steps, each a full model round trip. This returns
the shape of the project (top-level areas with file counts, languages, entry points) and the head of its manifests
and README in a single step, built from the device-side ops the other file tools already use (fs.list, fs.read);
no companion change is needed. Read-only.
"""

import asyncio
from collections import Counter
from typing import Optional

from .file_ops import _cb, _pkg, _remote_uid, _ws_resolve, active_workspace

# files worth reading the head of, matched on the lower-cased file name
MANIFESTS = (
    "readme.md", "readme.rst", "readme.txt", "readme", "package.json", "pyproject.toml", "requirements.txt",
    "setup.py", "setup.cfg", "pipfile", "cargo.toml", "go.mod", "composer.json", "pom.xml", "build.gradle",
    "build.gradle.kts", "gemfile", "makefile", "dockerfile", "docker-compose.yml", "tsconfig.json", "vite.config.js",
    "vite.config.ts", "next.config.js", "agents.md", "claude.md", ".cursorrules",
)
ENTRY_NAMES = ("main", "app", "index", "server", "cli", "manage", "run", "__main__")
HEAD_LINES = 40
HEAD_CHARS = 2500
MAX_MANIFESTS = 8
SOURCE_EXTS = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt", ".cs", ".php", ".rb", ".c", ".cc",
               ".cpp", ".h", ".hpp", ".swift", ".vue", ".svelte", ".html", ".css", ".scss", ".sql", ".sh", ".ps1"}


def _ext(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""


def summarize(files: list) -> dict:
    """Pure: top-level areas, language counts, entry points and manifests from a (partial) list of relative paths."""
    areas: Counter = Counter()
    exts: Counter = Counter()
    entries, manifests = [], []
    for f in files:
        parts = f.split("/")
        areas[parts[0] if len(parts) > 1 else "(root)"] += 1
        e = _ext(f)
        if e in SOURCE_EXTS:
            exts[e] += 1
        base = parts[-1].lower()
        depth = len(parts)
        if base in MANIFESTS and depth <= 3 and f not in manifests:
            manifests.append(f)
        stem = base.rsplit(".", 1)[0]
        if e in SOURCE_EXTS and stem in ENTRY_NAMES and depth <= 3:
            entries.append(f)
    # shallow manifests first: the root README / package.json say the most
    manifests.sort(key=lambda p: (p.count("/"), p))
    return {"areas": areas, "exts": exts, "entries": entries[:12], "manifests": manifests}


def _head(text: str) -> str:
    out = "\n".join(text.splitlines()[:HEAD_LINES])
    return out[:HEAD_CHARS] + ("\n..." if len(out) > HEAD_CHARS or len(text.splitlines()) > HEAD_LINES else "")


async def tool_project_overview(args: dict) -> str:
    ws = getattr(_pkg(), "active_workspace", active_workspace)()
    sub = str((args or {}).get("path") or "").strip().strip("/\\")
    root = getattr(_pkg(), "_ws_resolve", _ws_resolve)(sub) if sub else ws
    uid = getattr(_pkg(), "_remote_uid", _remote_uid)()
    cb = _cb()

    # fs.list caps each call at 200 files, so ask for several depths in parallel to see more of the tree
    patterns = ("*", "*/*", "*/*/*", "*/*/*/*")
    results = await asyncio.gather(
        *[cb.call(uid, "fs.list", {"root": str(root), "pattern": p}) for p in patterns], return_exceptions=True)
    files: list = []
    capped = False
    for r in results:
        if isinstance(r, Exception):
            continue
        got = r.get("files") or []
        capped = capped or len(got) >= 200
        for f in got:
            if f not in files:
                files.append(f)
    if not files:
        return "(no files found - is the workspace folder empty?)"

    s = summarize(files)
    lines = [f"Project overview of {sub or '.'} ({len(files)} files sampled" + (", the tree is larger than shown)" if capped else ")")]
    lines.append("Top-level areas (files seen): " + ", ".join(f"{a} ({n})" for a, n in s["areas"].most_common(15)))
    if s["exts"]:
        lines.append("Languages: " + ", ".join(f"{e} x{n}" for e, n in s["exts"].most_common(8)))
    if s["entries"]:
        lines.append("Likely entry points: " + ", ".join(s["entries"]))
    root_files = [f for f in files if "/" not in f]
    if root_files:
        lines.append("Root files: " + ", ".join(root_files[:40]))
    n_src = sum(s["exts"].values())
    lines.append(f"Source files seen: {n_src}" + (" (at least; the tree is capped)" if capped else "")
                 + ". For a large project split the areas across spawn_parallel_agents instead of reading everything yourself.")

    picks = s["manifests"][:MAX_MANIFESTS]

    async def _read(rel: str) -> Optional[str]:
        full = (root / rel) if hasattr(root, "joinpath") else f"{root}/{rel}"
        try:
            d = await cb.call(uid, "fs.read", {"path": str(full)})
        except Exception:
            return None
        c = d.get("content")
        return c if isinstance(c, str) else None

    heads = await asyncio.gather(*[_read(p) for p in picks])
    for rel, text in zip(picks, heads):
        if text:
            lines.append(f"\n--- {rel} (first {HEAD_LINES} lines) ---\n{_head(text)}")
    lines.append("\nNext: read the entry points and the few files that matter with parallel read_file calls in ONE step.")
    return "\n".join(lines)
