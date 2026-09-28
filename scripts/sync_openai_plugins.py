"""scripts/sync_openai_plugins.py - vendor skills from the openai/plugins marketplace
into skill_catalog/ for code review.

The Customize page never installs skills from the internet (core/skills.py): new
skills enter skill_catalog/ through a reviewed commit. This script does the fetch
and conversion so that review is of a plain git diff.

  python scripts/sync_openai_plugins.py --list
        show every plugin in the marketplace: category, skill count, MCP servers
  python scripts/sync_openai_plugins.py superpowers sentry cloudflare
        import all skills of those plugins into skill_catalog/
  python scripts/sync_openai_plugins.py superpowers:brainstorming,writing-plans
        import only the named skills of a plugin
  --dry-run   show what would be written, write nothing
  --update    replace skills this script imported earlier (never hand-written ones)
  --commit    upstream commit to pin (default PINNED_COMMIT below)

What it does, per skill:
  - name becomes <plugin>-<skill> (12 skill names collide across upstream plugins);
    kept as-is when the skill is named after its plugin (sentry/sentry -> sentry)
  - frontmatter is rewritten to this app's flat key: value form (name, title,
    description, triggers, category, author, version, source); the body is unchanged
  - every file of the skill folder is copied, plus the plugin/repo LICENSE when the
    skill has none; PROVENANCE.json records commit, tarball sha256 and file hashes
  - files that look like they hold a payment card number are NOT copied (PCI DSS)
MCP servers (.mcp.json) are only reported - they are remote OAuth HTTP servers that
send data outside Bangladesh and need a compliance decision before becoming presets.
ChatGPT app connectors (.app.json) cannot run here and are ignored.
"""

import argparse
import hashlib
import io
import json
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.pan import contains_pan  # noqa: E402
from core.skills import CATALOG_DIR, valid_name  # noqa: E402

REPO = "openai/plugins"
PINNED_COMMIT = "1dc195897af4161d039b80d8471ec0a10c9bbc89"
TARBALL_URL = "https://codeload.github.com/{repo}/tar.gz/{commit}"
MAX_TARBALL_BYTES = 300 * 1024 * 1024
MAX_FILE_BYTES = 1024 * 1024
SKIP_NAMES = {".DS_Store", "Thumbs.db"}
SKIP_SUFFIXES = {".pyc", ".pyo"}
EXEC_SUFFIXES = {".py", ".sh", ".js", ".mjs", ".cjs", ".ts", ".ps1", ".rb", ".bat"}
TEXT_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".toml", ".html",
                 ".xml", ".sql"} | EXEC_SUFFIXES
PROVENANCE = "PROVENANCE.json"


# ---------------- fetch ----------------

def fetch_tarball(commit: str) -> tuple[bytes, str]:
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        sys.exit("--commit must be a full 40-char commit sha (branches are not reviewable)")
    url = TARBALL_URL.format(repo=REPO, commit=commit)
    print(f"fetching {url}", file=sys.stderr)
    req = urllib.request.Request(url, headers={"User-Agent": "local-hybrid-agent/sync"})
    buf = io.BytesIO()
    with urllib.request.urlopen(req, timeout=120) as resp:
        while chunk := resp.read(1 << 20):
            buf.write(chunk)
            if buf.tell() > MAX_TARBALL_BYTES:
                sys.exit("tarball exceeds size cap")
    data = buf.getvalue()
    return data, hashlib.sha256(data).hexdigest()


class Repo:
    """Read-only view of the tarball: {repo-relative posix path: TarInfo}, regular files only."""

    def __init__(self, data: bytes):
        self.tar = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
        self.files = {}
        for m in self.tar.getmembers():
            if not m.isfile():          # symlinks, hardlinks, devices are never copied
                continue
            parts = PurePosixPath(m.name).parts[1:]    # drop "plugins-<sha>/"
            if not parts or any(p in ("", ".", "..") for p in parts) or m.name.startswith("/"):
                continue
            self.files["/".join(parts)] = m

    def read(self, path: str) -> bytes | None:
        m = self.files.get(path)
        return self.tar.extractfile(m).read() if m else None

    def json(self, path: str) -> dict:
        raw = self.read(path)
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {}

    def under(self, prefix: str) -> list[str]:
        return sorted(p for p in self.files if p.startswith(prefix))


# ---------------- marketplace model ----------------

def plugins_index(repo: Repo) -> dict:
    market = repo.json(".agents/plugins/marketplace.json")
    out = {}
    for e in market.get("plugins") or []:
        src = e.get("source") or {}
        if not isinstance(e, dict) or src.get("source") != "local":
            continue
        base = str(src.get("path") or "").strip("./").strip("/")
        if not base.startswith("plugins/"):
            continue
        manifest = repo.json(f"{base}/.codex-plugin/plugin.json")
        skills = sorted({p[len(base) + 8:].split("/")[0] for p in repo.under(f"{base}/skills/")
                         if p.endswith("/SKILL.md") and p.count("/") == base.count("/") + 3})
        mcp = (repo.json(f"{base}/.mcp.json").get("mcpServers") or {})
        out[e["name"]] = {"base": base, "category": e.get("category") or "general",
                          "manifest": manifest, "skills": skills, "mcp": mcp}
    return out


def target_name(plugin: str, skill: str, shared: set) -> str:
    """<plugin>-<skill>; the bare skill name when it already carries the plugin name, or
    when the prefixed form breaks the length rule and the skill name is unique upstream."""
    if skill == plugin or skill.startswith(plugin + "-"):
        return skill
    name = f"{plugin}-{skill}"
    if not valid_name(name) and skill not in shared:
        return skill
    return name


def shared_skill_names(index: dict) -> set:
    seen, shared = set(), set()
    for info in index.values():
        for s in info["skills"]:
            (shared if s in seen else seen).add(s)
    return shared


# ---------------- SKILL.md conversion ----------------

def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Minimal YAML subset: top-level scalars, quoted strings, | and > block scalars,
    indented continuation lines. Nested maps/lists are ignored."""
    m = re.match(r"^---[ \t]*\n([\s\S]*?)\n---[ \t]*\n?([\s\S]*)$", text)
    if not m:
        return {}, text
    meta, key, block = {}, None, []

    def flush():
        if key is not None:
            meta[key] = " ".join(s.strip() for s in block if s.strip())

    for line in m.group(1).splitlines():
        top = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if top:
            flush()
            key, val = top.group(1).lower(), top.group(2).strip()
            block = [] if val in ("|", ">", "|-", ">-", "") else [val]
        elif key is not None and line[:1] in (" ", "\t"):
            block.append(line)
    flush()
    for k, v in meta.items():
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            meta[k] = v[1:-1].replace('\\"', '"')
    return meta, m.group(2)


def one_line(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def convert_skill_md(text: str, *, name: str, plugin: str, skill: str, info: dict,
                     commit: str) -> str:
    meta, body = parse_frontmatter(text.replace("\r\n", "\n"))
    manifest = info["manifest"]
    display = (manifest.get("interface") or {}).get("displayName") or plugin
    author = manifest.get("author")
    author = author.get("name") if isinstance(author, dict) else author
    desc = one_line(meta.get("description")) or f"{display} skill: {skill}"
    front = {
        "name": name,
        "title": (display if skill == plugin else
                  f"{display}: {skill.replace('-', ' ').title()}")[:80],
        "description": desc,
        "triggers": ", ".join(dict.fromkeys([skill.replace("-", " "), name, f"/{name}"])),
        "category": one_line(info["category"]).lower()[:40],
        "author": one_line(f"{author or 'OpenAI'} (via {REPO})")[:80],
        "version": one_line(manifest.get("version") or "")[:20],
        "source": f"{REPO}@{commit[:12]}:{info['base']}/skills/{skill}",
    }
    head = "\n".join(f"{k}: {v}" for k, v in front.items() if v)
    return f"---\n{head}\n---\n\n{body.lstrip()}"


# ---------------- import ----------------

def license_for(repo: Repo, info: dict, skill_prefix: str) -> tuple[str, bytes] | None:
    for base in (skill_prefix, info["base"] + "/", ""):
        for fn in ("LICENSE", "LICENSE.txt", "LICENSE.md"):
            data = repo.read(base + fn)
            if data:
                return base + fn, data
    return None


def import_skill(repo: Repo, plugin: str, skill: str, info: dict, *, commit: str,
                 tar_sha: str, dry_run: bool, update: bool, shared: set) -> dict:
    name = target_name(plugin, skill, shared)
    report = {"plugin": plugin, "skill": skill, "name": name, "status": "", "notes": []}
    if not valid_name(name):
        report["status"] = f"skipped: '{name}' breaks the name rule ^[a-z0-9][a-z0-9_-]{{0,40}}$"
        return report
    dest = CATALOG_DIR / name
    if dest.exists():
        prov = {}
        try:
            prov = json.loads((dest / PROVENANCE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        if prov.get("repo") != REPO:
            report["status"] = "skipped: a hand-written catalog skill already uses this name"
            return report
        if not update:
            report["status"] = "skipped: already imported (use --update to replace)"
            return report

    prefix = f"{info['base']}/skills/{skill}/"
    files, hashes = {}, {}
    for path in repo.under(prefix):
        rel = path[len(prefix):]
        pp = PurePosixPath(rel)
        if pp.name in SKIP_NAMES or pp.suffix in SKIP_SUFFIXES or "__pycache__" in pp.parts:
            continue
        m = repo.files[path]
        if m.size > MAX_FILE_BYTES:
            report["notes"].append(f"not copied (> 1 MiB): {rel}")
            continue
        data = repo.read(path)
        if pp.suffix.lower() in TEXT_SUFFIXES and contains_pan(data.decode("utf-8", "replace")):
            report["notes"].append(f"NOT COPIED - looks like it contains a card number: {rel}")
            continue
        if rel == "SKILL.md":
            data = convert_skill_md(data.decode("utf-8", "replace"), name=name, plugin=plugin,
                                    skill=skill, info=info, commit=commit).encode("utf-8")
        files[rel] = data
        hashes[rel] = hashlib.sha256(data).hexdigest()
    if "SKILL.md" not in files:
        report["status"] = "skipped: SKILL.md missing or not copied"
        return report

    lic = license_for(repo, info, prefix)
    has_own = any(PurePosixPath(r).name.upper().startswith("LICENSE") for r in files)
    if lic and not has_own:
        files["LICENSE.upstream"] = lic[1]
        hashes["LICENSE.upstream"] = hashlib.sha256(lic[1]).hexdigest()
    if not lic:
        declared = info["manifest"].get("license")
        report["notes"].append("no LICENSE file upstream" + (f" (plugin.json declares '{declared}')"
                               if declared else "") + " - check terms before committing")
    executables = sorted(r for r in files if PurePosixPath(r).suffix.lower() in EXEC_SUFFIXES)
    if executables:
        report["notes"].append(f"{len(executables)} bundled script(s) - never run server-side; "
                               "agent tools run on the user's device only")
    manifest = info["manifest"]
    provenance = {
        "repo": REPO, "commit": commit, "tarball_sha256": tar_sha,
        "plugin": plugin, "plugin_version": manifest.get("version"),
        "plugin_license": manifest.get("license"), "upstream_path": prefix.rstrip("/"),
        "license_file": lic[0] if lic else None, "bundled_scripts": executables,
        "files": hashes,
    }
    files[PROVENANCE] = (json.dumps(provenance, indent=2) + "\n").encode("utf-8")

    report["files"] = len(files)
    if dry_run:
        report["status"] = "would write" + (" (replace)" if dest.exists() else "")
        return report
    tmp = Path(tempfile.mkdtemp(prefix=f".{name}-", dir=CATALOG_DIR))
    try:
        for rel, data in files.items():
            out = (tmp / rel).resolve()
            if tmp.resolve() not in out.parents:
                raise ValueError(f"path escapes skill dir: {rel}")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
        if dest.exists():
            shutil.rmtree(dest)
        tmp.rename(dest)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    report["status"] = "replaced" if update else "written"
    return report


# ---------------- cli ----------------

def parse_selection(args: list[str]) -> dict:
    sel = {}
    for a in args:
        plugin, _, skills = a.partition(":")
        sel.setdefault(plugin.strip(), set()).update(s.strip() for s in skills.split(",") if s.strip())
    return sel


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("plugins", nargs="*", help="plugin or plugin:skill1,skill2")
    ap.add_argument("--list", action="store_true", help="list marketplace plugins and exit")
    ap.add_argument("--commit", default=PINNED_COMMIT)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--update", action="store_true")
    a = ap.parse_args()
    if not a.list and not a.plugins:
        ap.error("name at least one plugin, or use --list")

    data, tar_sha = fetch_tarball(a.commit)
    repo = Repo(data)
    index = plugins_index(repo)
    print(f"{REPO}@{a.commit[:12]}  tarball sha256 {tar_sha}  {len(index)} plugins\n")

    if a.list:
        for name, info in sorted(index.items()):
            mcp = ",".join(info["mcp"]) or "-"
            print(f"{name:32} {info['category'][:22]:22} skills={len(info['skills']):3}  mcp={mcp}")
        return 0

    reports, mcp_seen, rc = [], {}, 0
    shared = shared_skill_names(index)
    for plugin, only in parse_selection(a.plugins).items():
        info = index.get(plugin)
        if not info:
            print(f"!! unknown plugin '{plugin}' (see --list)")
            rc = 1
            continue
        missing = only - set(info["skills"])
        if missing:
            print(f"!! {plugin} has no skill(s): {', '.join(sorted(missing))}")
            rc = 1
        if not info["skills"]:
            print(f"-- {plugin}: no skills upstream (only connectors/apps)")
        for skill in info["skills"]:
            if only and skill not in only:
                continue
            reports.append(import_skill(repo, plugin, skill, info, commit=a.commit,
                                        tar_sha=tar_sha, dry_run=a.dry_run, update=a.update,
                                        shared=shared))
        for sid, cfg in info["mcp"].items():
            mcp_seen[f"{plugin}/{sid}"] = cfg

    for r in reports:
        print(f"{r['name']:42} {r['status']}" + (f"  ({r['files']} files)" if r.get("files") else ""))
        for n in r["notes"]:
            print(f"{'':44}- {n}")
    if mcp_seen:
        print("\nMCP servers in these plugins (NOT added - remote egress, needs compliance sign-off):")
        for sid, cfg in mcp_seen.items():
            print(f"  {sid:30} {cfg.get('type', 'stdio'):6} {cfg.get('url') or cfg.get('command') or ''}")
    written = [r for r in reports if r["status"] in ("written", "replaced")]
    if written:
        print(f"\n{len(written)} skill(s) in skill_catalog/. Review with: git diff --stat -- skill_catalog/")
    return rc


if __name__ == "__main__":
    sys.exit(main())
