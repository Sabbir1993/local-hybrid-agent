import json
import re
from pathlib import Path
from typing import Optional
from .constants import ALWAYS_TOOLS, TOOL_MAP, _COMPAT_CHECKS


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    m = re.match(r"^---\s*\n([\s\S]*?)\n---\s*\n?([\s\S]*)$", text)
    if not m:
        return {}, text
    front, body = m.group(1), m.group(2)
    meta = {}
    for line in front.splitlines():
        fm = re.match(r"([\w-]+)\s*:\s*(.*)$", line)
        if fm:
            val = fm.group(2).strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            meta[fm.group(1).lower()] = val
    return meta, body


def _parse_tools(val: str) -> list:
    val = (val or "").strip()
    if val.startswith("["):
        try:
            items = json.loads(val)
            return [str(t).strip() for t in items if str(t).strip()]
        except ValueError:
            val = val.strip("[]")
    return [t.strip().strip("\"'") for t in val.split(",") if t.strip()]


def _map_tools(names: list) -> list:
    out = []
    for n in names:
        for t in TOOL_MAP.get(n.lower().replace("_", ""), []):
            if t not in out:
                out.append(t)
    for t in ALWAYS_TOOLS:
        if t not in out:
            out.append(t)
    return out


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _compat(text: str) -> list:
    return [msg for rx, msg in _COMPAT_CHECKS if rx.search(text)]
