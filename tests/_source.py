"""Read the source of a module that may have been split into a package.

Structural tests grep the code of a route or core module; once `routes/agent.py`
becomes `routes/agent/`, they need the text of every file in the package.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def module_files(rel: str) -> list:
    """`routes/agent.py` -> [that file] or every .py in `routes/agent/`, sorted."""
    p = REPO / rel
    if p.is_file():
        return [p]
    pkg = p.with_suffix("")
    if pkg.is_dir():
        return sorted(pkg.rglob("*.py"))
    raise FileNotFoundError(rel)


def module_source(rel: str) -> str:
    return "\n".join(f.read_text(encoding="utf-8") for f in module_files(rel))
