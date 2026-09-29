from pathlib import Path
import re
import sys
from ..config import BASE_DIR

PLUGINS_DIR = BASE_DIR / "plugins"
CATALOG_DIR = BASE_DIR / "plugin_catalog"

MAX_PROMPT_FRAG_CHARS = 1500
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")


def _get_plugins_dir() -> Path:
    return getattr(sys.modules.get("core.plugins"), "PLUGINS_DIR", PLUGINS_DIR)


def _get_catalog_dir() -> Path:
    return getattr(sys.modules.get("core.plugins"), "CATALOG_DIR", CATALOG_DIR)


def _caps() -> dict:
    from ..small_model import APP_CONFIG
    return APP_CONFIG.setdefault("capabilities", {})


def plugins_enabled() -> bool:
    return bool(_caps().get("plugins", False))


def disabled_names() -> set:
    return set(_caps().get("plugins_disabled") or [])


def valid_name(name: str) -> bool:
    return bool(name) and bool(NAME_RE.match(name))
