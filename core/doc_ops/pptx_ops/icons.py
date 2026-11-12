"""Icons for cards, stats and process steps, drawn with PowerPoint's own shapes.

No picture files and no licence to track: an icon is a native shape tinted with the theme colour, so it stays sharp at any
size and the user can recolour or replace it in PowerPoint. The outline names one with `icon=<name>` on an item:

    - icon=growth | Revenue | up 18% on last quarter

Unknown names are matched by keyword (ALIASES) and otherwise left out; the caller reports that in the build notes."""
import re

from pptx.enum.shapes import MSO_SHAPE

ICONS = {
    "star": MSO_SHAPE.STAR_5_POINT, "idea": MSO_SHAPE.SUN, "speed": MSO_SHAPE.LIGHTNING_BOLT, "cloud": MSO_SHAPE.CLOUD,
    "settings": MSO_SHAPE.GEAR_6, "data": MSO_SHAPE.CAN, "document": MSO_SHAPE.FOLDED_CORNER, "people": MSO_SHAPE.SMILEY_FACE,
    "growth": MSO_SHAPE.UP_ARROW, "decline": MSO_SHAPE.DOWN_ARROW, "next": MSO_SHAPE.RIGHT_ARROW, "add": MSO_SHAPE.MATH_PLUS,
    "risk": MSO_SHAPE.NO_SYMBOL, "heart": MSO_SHAPE.HEART, "product": MSO_SHAPE.CUBE, "target": MSO_SHAPE.DONUT,
    "filter": MSO_SHAPE.FUNNEL, "cycle": MSO_SHAPE.CIRCULAR_ARROW, "exchange": MSO_SHAPE.LEFT_RIGHT_ARROW,
    "share": MSO_SHAPE.PIE, "message": MSO_SHAPE.OVAL_CALLOUT, "alert": MSO_SHAPE.EXPLOSION1, "decision": MSO_SHAPE.DIAMOND,
    "network": MSO_SHAPE.HEXAGON, "expand": MSO_SHAPE.QUAD_ARROW, "home": MSO_SHAPE.ACTION_BUTTON_HOME,
    "info": MSO_SHAPE.ACTION_BUTTON_INFORMATION, "help": MSO_SHAPE.ACTION_BUTTON_HELP, "night": MSO_SHAPE.MOON,
    "drop": MSO_SHAPE.TEAR, "wave": MSO_SHAPE.WAVE, "award": MSO_SHAPE.STAR_8_POINT, "cross": MSO_SHAPE.MATH_MULTIPLY,
}
ALIASES = {
    "rocket": "growth", "up": "growth", "increase": "growth", "revenue": "growth", "trend": "growth", "profit": "growth",
    "down": "decline", "decrease": "decline", "loss": "decline", "cost": "decline",
    "light": "idea", "lightbulb": "idea", "bulb": "idea", "innovation": "idea", "insight": "idea",
    "fast": "speed", "bolt": "speed", "energy": "speed", "performance": "speed", "power": "speed",
    "gear": "settings", "config": "settings", "process": "settings", "operations": "settings", "automation": "settings",
    "tool": "settings", "tools": "settings", "engineering": "settings",
    "database": "data", "storage": "data", "server": "data", "analytics": "share", "chart": "share", "market": "share",
    "file": "document", "report": "document", "contract": "document", "policy": "document", "compliance": "document",
    "user": "people", "users": "people", "team": "people", "customer": "people", "customers": "people", "staff": "people",
    "arrow": "next", "step": "next", "forward": "next", "plus": "add", "new": "add",
    "warning": "alert", "incident": "alert", "block": "risk", "security": "risk", "fraud": "risk", "stop": "risk",
    "love": "heart", "health": "heart", "care": "heart", "box": "product", "package": "product", "delivery": "product",
    "goal": "target", "focus": "target", "kpi": "target", "funnel": "filter", "conversion": "filter",
    "refresh": "cycle", "loop": "cycle", "recurring": "cycle", "sync": "exchange", "transfer": "exchange",
    "payment": "exchange", "payments": "exchange", "transaction": "exchange", "integration": "network", "api": "network",
    "chat": "message", "support": "message", "feedback": "message", "comment": "message", "question": "help",
    "choice": "decision", "option": "decision", "global": "expand", "scale": "expand", "reach": "expand",
    "quality": "award", "best": "award", "winner": "star", "favourite": "star", "favorite": "star", "rating": "star",
    "water": "drop", "bank": "home", "office": "home", "branch": "home", "about": "info", "details": "info",
    "remove": "cross", "close": "cross", "cancel": "cross",
}
NAMES = sorted(ICONS)
_ICON_RX = re.compile(r"(?i)(?:^|\|)\s*icon\s*=\s*([a-z0-9 _-]{1,24}?)\s*(?=\||$)")


def resolve(name: str):
    """-> (canonical name, shape) or (None, None)."""
    key = re.sub(r"[^a-z0-9]", "", str(name or "").lower())
    key = key if key in ICONS else ALIASES.get(key)
    return (key, ICONS[key]) if key else (None, None)


def take(text: str) -> tuple:
    """Split an `icon=name` field off an item: 'icon=growth | Revenue | up 18%' -> ('Revenue | up 18%', 'growth').
    The name is returned as written ('' when the item has none)."""
    m = _ICON_RX.search(str(text or ""))
    if not m:
        return text, ""
    rest = (text[:m.start()] + ("|" if m.start() > 0 and m.end() < len(text) else "") + text[m.end():]).strip().strip("|").strip()
    return rest, m.group(1).strip()
