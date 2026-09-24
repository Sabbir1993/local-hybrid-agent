"""Skills capability: .agents/skills/<name>/SKILL.md convention.

Frontmatter (--- delimited, simple key: value lines): name, description, triggers.
Body: markdown instructions. Skill names + one-liners are injected into the
agent system prompt (token-cheap); the model pulls full bodies via read_skill.

Catalog: skill_catalog/<name>/SKILL.md is the curated, in-repo (reviewed) set the
Customize page can browse and install. Installing copies the folder into
.agents/skills/<name>/. A skill body becomes agent instructions, so - like plugins -
nothing is downloaded from the internet; new skills enter the catalog via code review.
"""

import hashlib
import re
import shutil
import sys
from pathlib import Path
from typing import Optional

from .config import BASE_DIR
from .registry import registry

SKILLS_DIR = BASE_DIR / ".agents" / "skills"
CATALOG_DIR = BASE_DIR / "skill_catalog"
MAX_SKILL_BODY_CHARS = 12000   # safety cap for read_skill output

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")


def parse_skill_md(path: Path) -> Optional[dict]:
    """Parse one SKILL.md -> {name, description, triggers, body} or None."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    name = path.parent.name
    description = ""
    triggers = []
    meta = {}
    body = text

    m = re.match(r"^---\s*\n([\s\S]*?)\n---\s*\n?([\s\S]*)$", text)
    if m:
        front, body = m.group(1), m.group(2)
        for line in front.splitlines():
            fm = re.match(r"(\w+)\s*:\s*(.*)$", line)
            if not fm:
                continue
            key, val = fm.group(1).lower(), fm.group(2).strip()
            if key == "name":
                name = val
            elif key == "description":
                description = val
            elif key == "triggers":
                triggers = [t.strip() for t in re.split(r"[,;]", val) if t.strip()]
            elif key in ("category", "author", "version", "title"):
                meta[key] = val
    if not description:
        # first non-empty body line as fallback description
        for ln in body.splitlines():
            if ln.strip():
                description = ln.strip().lstrip("# ")
                break
    return {
        "name": name,
        "description": description[:200],
        "triggers": triggers,
        "body": body,
        "path": str(path),
        "title": meta.get("title", "")[:80],
        "category": (meta.get("category") or "general")[:40],
        "author": meta.get("author", "")[:80],
        "version": meta.get("version", "")[:20],
    }


def load_skills() -> dict:
    """All discovered skills: {dir_name: skill_dict}.
    Stored globally for the entire application in the runtime .agents/skills/ directory.
    """
    out = {}
    if not SKILLS_DIR.is_dir():
        SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        return out

    for d in sorted(SKILLS_DIR.iterdir()):
        if not d.is_dir():
            continue
        sk = parse_skill_md(d / "SKILL.md")
        if sk:
            out[d.name] = sk
    return out


def skills_prompt_fragment() -> str:
    """Short skills listing appended to the agent system prompt."""
    from .small_model import APP_CONFIG
    if not APP_CONFIG.get("capabilities", {}).get("skills", False):
        return ""
    skills = load_skills()
    if not skills:
        return ""
    lines = [
        "",
        "Available Skills:",
        "When the user's request starts with or mentions one or more skill names, slash commands, or triggers (e.g. /grill-me, /goal, /code-review, /<skill-name>):",
        "- You MUST call read_skill(<name>) on your FIRST step for EACH skill mentioned in the request.",
        "- If multiple skills or commands are chained in a single request (e.g. [/grill-me ... /goal ...]): follow their instructions in sequence. First execute the earlier phase (e.g. ask clarifying interview questions for /grill-me), and then adhere to subsequent skills/guidelines (e.g. autonomous production readiness for /goal).",
    ]
    for sk in skills.values():
        lines.append(f"- {sk['name']}: {sk['description']}")
    return "\n".join(lines)


def tool_list_skills(args: dict) -> str:
    skills = load_skills()
    if not skills:
        return "(no skills found in .agents/skills/ directory)"
    return "\n".join(f"- {s['name']}: {s['description']}" for s in skills.values())


def tool_read_skill(args: dict) -> str:
    want = (args.get("name") or args.get("skill") or "").strip().lower()
    if not want:
        raise ValueError("name required")
    skills = load_skills()
    for key, sk in skills.items():
        if want in (key.lower(), sk["name"].lower()) or want == sk["name"].lower():
            body = sk["body"]
            if len(body) > MAX_SKILL_BODY_CHARS:
                body = body[:MAX_SKILL_BODY_CHARS] + "\n... (truncated)"
            return f"# Skill: {sk['name']}\n\n{body}"
    available = ", ".join(s["name"] for s in skills.values()) or "(none)"
    return f"error: skill '{args.get('name')}' not found. Available: {available}"


# ---------------- catalog ----------------

def valid_name(name: str) -> bool:
    return bool(name) and bool(NAME_RE.match(name))


def _tree_hash(d: Path) -> Optional[str]:
    """sha256 over every file (relative path + bytes) so extra reference files count too."""
    if not d.is_dir():
        return None
    h = hashlib.sha256()
    try:
        for p in sorted(d.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                h.update(p.relative_to(d).as_posix().encode())
                h.update(p.read_bytes())
    except OSError:
        return None
    return h.hexdigest()


def _catalog_dir(name: str) -> Optional[Path]:
    d = CATALOG_DIR / name
    return d if valid_name(name) and (d / "SKILL.md").is_file() else None


def catalog_entries() -> list:
    """Curated skills shipped in skill_catalog/ (only dirs with SKILL.md)."""
    out = []
    if not CATALOG_DIR.is_dir():
        return out
    for d in sorted(CATALOG_DIR.iterdir()):
        if not _catalog_dir(d.name):
            continue
        sk = parse_skill_md(d / "SKILL.md")
        if not sk:
            continue
        out.append({
            "name": d.name,
            "title": sk["title"] or sk["name"],
            "description": sk["description"],
            "category": sk["category"],
            "author": sk["author"],
            "version": sk["version"],
            "triggers": sk["triggers"][:10],
            "sha256": _tree_hash(d),
        })
    return out


def is_installed(name: str) -> bool:
    return valid_name(name) and (SKILLS_DIR / name / "SKILL.md").is_file()


def is_modified(name: str) -> bool:
    """True if the installed skill differs from its catalog copy (or has none)."""
    src = _catalog_dir(name)
    if not src:
        return True
    return _tree_hash(src) != _tree_hash(SKILLS_DIR / name)


def install_from_catalog(name: str) -> None:
    """Copy skill_catalog/<name> into .agents/skills/<name>. Raises ValueError on refusal."""
    src = _catalog_dir(name)
    if not src:
        raise ValueError(f"'{name}' is not in the skill catalog")
    dst = SKILLS_DIR / name
    if dst.exists():
        if not is_modified(name):
            return  # already installed, identical
        raise ValueError(f".agents/skills/{name} already exists with local changes — remove it manually first")
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def uninstall(name: str) -> None:
    """Remove .agents/skills/<name>. Only unmodified catalog skills can be removed here;
    hand-written skills stay on disk."""
    if not is_installed(name):
        raise ValueError(f"'{name}' is not installed")
    if is_modified(name):
        raise ValueError(f"'{name}' is hand-written or locally modified — remove .agents/skills/{name} manually")
    shutil.rmtree(SKILLS_DIR / name)


def register_skill_tools() -> None:
    from .small_model import APP_CONFIG
    if not APP_CONFIG.get("capabilities", {}).get("skills", False):
        return
    registry.register(
        "list_skills", tool_list_skills,
        {"type": "function", "function": {
            "name": "list_skills",
            "description": "List available skills (reusable instruction packs in .agents/skills/).",
            "parameters": {"type": "object", "properties": {}},
        }},
        source="skill", meta={"label": "List skills"}, replace=True)
    registry.register(
        "read_skill", tool_read_skill,
        {"type": "function", "function": {
            "name": "read_skill",
            "description": "Load the full instructions of a skill by name. Read this before following a skill.",
            "parameters": {"type": "object",
                           "properties": {"name": {"type": "string", "description": "skill name from list_skills"}},
                           "required": ["name"]},
        }},
        source="skill", meta={"label": "Read skill"}, replace=True)
