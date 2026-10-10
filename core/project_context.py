"""
core/project_context.py - Per-project instructions file (AGENTS.md), the
equivalent of Claude Code's CLAUDE.md / Codex's AGENTS.md.

`/init` (agent mode) asks the agent to scan the active workspace and write
AGENTS.md; every later agent / sub-agent run in that project gets the file
injected into its system prompt, so the agent starts out knowing the build
commands, architecture and conventions instead of re-exploring each time.

The file is read on the user's machine through the companion (policy-gated fs.read). It is user-editable and may reach a cloud lane, so card numbers and
obvious credentials are masked before it enters a prompt.
"""

import re
from typing import Optional

from . import pan

INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md")
MAX_CHARS = 8000

_SECRET_RXS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[0-9A-Za-z]{16,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]{20,}=*"),
    # key = value / key: value where the key names a secret
    re.compile(r"(?i)\b((?:api[_-]?key|secret|password|passwd|pwd|token|store[_-]?passw(?:or)?d|"
               r"client[_-]?secret|access[_-]?key)\w*\s*[:=]\s*)(['\"]?)[^\s'\"`]{6,}\2"),
]

INIT_PROMPT = """Initialize this project for future agent sessions: create (or improve) an AGENTS.md file at the workspace root.

Steps:
1. Explore FAST. Your first action is ONE project_overview call (layout, languages, entry points, README and manifest heads). Then read what is still missing (CI config such as .github/workflows or .gitlab-ci.yml, test setup, entry points, any existing AGENTS.md, CLAUDE.md, .cursorrules, .cursor/rules or .github/copilot-instructions.md) with several read_file / grep calls TOGETHER in a single step, never one per step. Read at most about 10 files and never read the same file or range twice: note what you learn as you go. If project_overview shows a large project (about 150+ source files, or three or more separate code areas), do not read it all yourself: call spawn_parallel_agents with one read-only sub-agent per area (for example backend, frontend, config and tests), each asked to return a short summary of its area (purpose, entry points, commands, conventions), then build AGENTS.md from those summaries.
2. Write AGENTS.md with these sections, only what you actually found:
   - Overview: what the project is, main language/framework (2-4 lines).
   - Commands: install, build, run/dev, lint, test, and how to run a single test.
   - Architecture: the big picture that needs several files to understand (entry points, main modules, data flow). Do not list every file.
   - Conventions & gotchas: non-obvious rules, env/config requirements, things that break easily.
3. If AGENTS.md already exists, use edit_file to improve it instead of overwriting it; fold in useful content from the other rule files.

Rules: be concise (aim for under 150 lines), no generic advice ("write clean code", "add tests"), do not invent commands you did not see. Never copy secrets, credentials, tokens, keys or card numbers into the file - write [PLACEHOLDER] instead.
When done, reply with a short summary of what AGENTS.md contains."""


def sanitize(text: str) -> str:
    text, _ = pan.mask_pans(text)
    for rx in _SECRET_RXS:
        if rx.groups >= 2:
            text = rx.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]{m.group(2)}", text)
        else:
            text = rx.sub("[REDACTED]", text)
    return text


def _clip(text: str) -> str:
    if len(text) <= MAX_CHARS:
        return text
    return text[:MAX_CHARS] + f"\n... (truncated, {len(text)} chars total — keep AGENTS.md short)"


RULE_DIRS = (".agents/rules", ".cursor/rules")
RULE_SUFFIXES = (".md", ".mdc")
MAX_RULE_FILES = 12
MAX_RULES_CHARS = 6000
_FRONTMATTER_RX = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.S)


def _rule_text(raw: str) -> str:
    return _FRONTMATTER_RX.sub("", raw.strip(), count=1).strip()       # Cursor .mdc files start with YAML metadata


async def load_rule_files() -> list:
    """[(path, sanitized text)] for the project's rule files (.agents/rules/*.md, .cursor/rules/*.md[c]), read on
    the user's machine. Small, many, topic-focused files next to AGENTS.md; at most MAX_RULE_FILES, MAX_RULES_CHARS in total."""
    import sys
    from .agent_tools import active_workspace, read_raw, _remote_uid
    cb = getattr(sys.modules.get("core.agent_tools"), "companion_bridge", None)
    if cb is None:
        return []
    try:
        root, uid = str(active_workspace()), _remote_uid()
    except Exception:
        return []
    found: list = []
    for d in RULE_DIRS:
        for suffix in RULE_SUFFIXES:
            try:
                data = await cb.call(uid, "fs.list", {"root": root, "pattern": f"{d}/*{suffix}"})
            except Exception:
                continue
            found.extend(f.replace("\\", "/") for f in (data.get("files") or []) if isinstance(f, str))
    out, used = [], 0
    for path in sorted(set(found))[:MAX_RULE_FILES]:
        try:
            raw = await read_raw(path)
        except Exception:
            continue
        text = _rule_text(raw or "")
        if not text:
            continue
        text = sanitize(text)[:max(0, MAX_RULES_CHARS - used)]
        if not text:
            break
        used += len(text)
        out.append((path, text))
    return out


async def load_project_instructions() -> Optional[tuple[str, str]]:
    """(filename, sanitized text) for the active project, or None. AGENTS.md (or CLAUDE.md) first, then the rule
    files from the rules folders, each under its own heading."""
    from .agent_tools import read_raw
    name, text = None, ""
    for candidate in INSTRUCTION_FILES:
        try:
            raw = await read_raw(candidate)
        except Exception:
            continue
        if raw and raw.strip():
            name, text = candidate, _clip(sanitize(raw.strip()))
            break
    rules = await load_rule_files()
    if rules:
        text += "".join(f"\n\n### Rule: {path}\n{body}" for path, body in rules)
        name = name or "the project rules folder"
    return (name, text.strip()) if name else None


def prompt_block(pi: Optional[tuple[str, str]]) -> str:
    if not pi:
        return ""
    name, text = pi
    return (f"\n\nPROJECT INSTRUCTIONS (from {name} in the workspace root — follow these; "
            f"they override generic defaults):\n{text}")
