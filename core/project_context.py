"""
core/project_context.py - Per-project instructions file (AGENTS.md), the
equivalent of Claude Code's CLAUDE.md / Codex's AGENTS.md.

`/init` (agent mode) asks the agent to scan the active workspace and write
AGENTS.md; every later agent / sub-agent run in that project gets the file
injected into its system prompt, so the agent starts out knowing the build
commands, architecture and conventions instead of re-exploring each time.

The file is read through tool_read_file, so a companion-backed workspace is
read on the user's machine (policy-gated fs.read) and a server-local one from
disk. It is user-editable and may reach a cloud lane, so card numbers and
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
1. Explore with list_files / read_file / grep: manifests (package.json, pyproject.toml, requirements*.txt, Cargo.toml, go.mod, composer.json, pom.xml, etc.), README, CI config (.github/workflows, .gitlab-ci.yml), test setup, and any existing AGENTS.md, CLAUDE.md, .cursorrules, .cursor/rules or .github/copilot-instructions.md.
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


async def load_project_instructions() -> Optional[tuple[str, str]]:
    """(filename, sanitized text) for the active project, or None."""
    from .agent_tools import tool_read_file
    for name in INSTRUCTION_FILES:
        try:
            raw = await tool_read_file({"path": name})
        except Exception:
            continue
        if not raw or raw.startswith("error:"):
            continue
        text = raw.strip()
        if text:
            return name, _clip(sanitize(text))
    return None


def prompt_block(pi: Optional[tuple[str, str]]) -> str:
    if not pi:
        return ""
    name, text = pi
    return (f"\n\nPROJECT INSTRUCTIONS (from {name} in the workspace root — follow these; "
            f"they override generic defaults):\n{text}")
