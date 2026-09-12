"""Skills capability: skills/<name>/SKILL.md convention.

Frontmatter (--- delimited, simple key: value lines): name, description, triggers.
Body: markdown instructions. Skill names + one-liners are injected into the
agent system prompt (token-cheap); the model pulls full bodies via read_skill.
"""

import re
import sys
from pathlib import Path
from typing import Optional

from .config import BASE_DIR
from .registry import registry

SKILLS_DIR = BASE_DIR / "skills"
MAX_SKILL_BODY_CHARS = 12000   # safety cap for read_skill output


def parse_skill_md(path: Path) -> Optional[dict]:
    """Parse one SKILL.md -> {name, description, triggers, body} or None."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    name = path.parent.name
    description = ""
    triggers = []
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
    }


def load_skills() -> dict:
    """All discovered skills: {dir_name: skill_dict}."""
    out = {}
    if not SKILLS_DIR.is_dir():
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
        "Available Skills (use read_skill to load full instructions before following one):",
    ]
    for sk in skills.values():
        lines.append(f"- {sk['name']}: {sk['description']}")
    return "\n".join(lines)


def tool_list_skills(args: dict) -> str:
    skills = load_skills()
    if not skills:
        return "(no skills found in skills/ directory)"
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


def register_skill_tools() -> None:
    from .small_model import APP_CONFIG
    if not APP_CONFIG.get("capabilities", {}).get("skills", False):
        return
    registry.register(
        "list_skills", tool_list_skills,
        {"type": "function", "function": {
            "name": "list_skills",
            "description": "List available skills (reusable instruction packs in skills/).",
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
