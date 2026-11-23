"""Per-user UI preferences, stored in auth.db next to the account itself.

Only a small, validated set of appearance keys is accepted; arbitrary JSON is
never trusted by the renderer. Theme colors are restricted to #RRGGBB.
"""
import json
import re
import time
from typing import Any, Dict

from . import auth_db

DEFAULTS: Dict[str, Any] = {
    "conversation_steps": "default",       # default | verbose
    "mode": "system",                      # system | light | dark
    "contrast": "default",                 # default | strong
    "font": "google",                      # google | system | mono
    "font_size": 14,                       # 12..20
    "theme": "slate",
    "background": "#070708",
    "foreground": "#FFFFFF",
    "accent": "#2F71E3",
}

_CHOICES = {
    "conversation_steps": {"default", "verbose"},
    "mode": {"system", "light", "dark"},
    "contrast": {"default", "strong"},
    "font": {"google", "system", "mono"},
    "theme": {"slate", "claude", "tokyonight", "oled", "nord", "classic", "ssl", "ssl-light", "light"},
}
_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def get(user_id: int) -> dict:
    row = auth_db.db().execute("SELECT settings FROM user_preferences WHERE user_id = ?", (user_id,)).fetchone()
    out = dict(DEFAULTS)
    if row:
        try:
            saved = json.loads(row["settings"] or "{}")
            if isinstance(saved, dict):
                out.update({k: v for k, v in saved.items() if k in DEFAULTS})
        except (ValueError, TypeError):
            pass
    return out


def save(user_id: int, values: dict) -> dict:
    current = get(user_id)
    for key, allowed in _CHOICES.items():
        if key in values and str(values[key]) in allowed:
            current[key] = str(values[key])
    if "font_size" in values:
        try:
            current["font_size"] = max(12, min(20, int(values["font_size"])))
        except (ValueError, TypeError):
            pass
    for key in ("background", "foreground", "accent"):
        if key in values and isinstance(values[key], str) and _COLOR.match(values[key].strip()):
            current[key] = values[key].strip()
    auth_db.db().execute(
        "INSERT INTO user_preferences (user_id, settings, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET settings=excluded.settings, updated_at=excluded.updated_at",
        (user_id, json.dumps(current), time.time()))
    auth_db.db().commit()
    return current
