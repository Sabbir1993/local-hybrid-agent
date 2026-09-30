from ..registry import registry
from .constants import DEVICES
from .tools import (
    tool_browser_click,
    tool_browser_close,
    tool_browser_console,
    tool_browser_eval,
    tool_browser_navigate,
    tool_browser_press,
    tool_browser_screenshot,
    tool_browser_select,
    tool_browser_snapshot,
    tool_browser_type,
    tool_browser_wait,
)


def _fn(name, desc, props, required=()):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": list(required)}}}


_TARGET_PROPS = {
    "ref": {"type": "string", "description": "element ref from the latest snapshot, e.g. e12"},
    "selector": {"type": "string", "description": "CSS selector (when there is no ref)"},
    "text": {"type": "string", "description": "visible text of the element (last resort)"},
}

BROWSER_TOOLS = [
    ("browser_navigate", tool_browser_navigate, _fn(
        "browser_navigate",
        "Open a URL in a real browser on the user's machine and return the page's accessibility snapshot "
        "(roles, names, [ref=eN] handles). Use it to check a web app you built or changed: start its dev server "
        "with run_shell first, then open http://localhost:<port>. Local dev hosts open directly; other sites ask "
        "the user. A browser window opens on the user's screen and stays open until browser_close. 'device' emulates a phone/tablet viewport (mobile web / responsive testing).",
        {"url": {"type": "string", "description": "e.g. http://localhost:5173/login"},
         "device": {"type": "string", "enum": DEVICES, "description": "viewport preset (default desktop)"},
         "show": {"type": "boolean", "description": "default: a visible window opens on the user's screen so they can watch; pass false to run headless"}},
        ["url"])),
    ("browser_snapshot", tool_browser_snapshot, _fn(
        "browser_snapshot", "Re-read the current page's accessibility snapshot (after it changed on its own).", {})),
    ("browser_click", tool_browser_click, _fn(
        "browser_click", "Click an element of the open page; returns the updated snapshot.",
        {**_TARGET_PROPS, "double": {"type": "boolean"}, "button": {"type": "string", "enum": ["left", "right"]}})),
    ("browser_type", tool_browser_type, _fn(
        "browser_type",
        "Type into an input of the open page (replaces its value). Refused for password and payment-card "
        "fields and for card-number-like values: ask the user to enter those.",
        {**_TARGET_PROPS, "value": {"type": "string", "description": "text to enter"},
         "submit": {"type": "boolean", "description": "press Enter afterwards"}},
        ["value"])),
    ("browser_press", tool_browser_press, _fn(
        "browser_press", "Press a key on the open page (Enter, Escape, Tab, ArrowDown, Control+A ...).",
        {"key": {"type": "string"}}, ["key"])),
    ("browser_select", tool_browser_select, _fn(
        "browser_select", "Choose option(s) in a <select> element.",
        {**_TARGET_PROPS, "values": {"type": "array", "items": {"type": "string"}}}, ["values"])),
    ("browser_wait", tool_browser_wait, _fn(
        "browser_wait", "Wait until some text appears on the page (or a short pause), then snapshot.",
        {"text": {"type": "string"}, "timeout_ms": {"type": "integer"}})),
    ("browser_screenshot", tool_browser_screenshot, _fn(
        "browser_screenshot",
        "Screenshot the open page: saved to .agent/screens/ in the project, shown to the user, and described by "
        "a vision model so you can spot visual/layout problems the snapshot can't show.",
        {"full_page": {"type": "boolean"},
         "question": {"type": "string", "description": "what to look for, e.g. 'is the login button visible?'"},
         "describe": {"type": "boolean", "description": "run the vision model (default true)"}})),
    ("browser_console", tool_browser_console, _fn(
        "browser_console",
        "Console errors/warnings, uncaught exceptions and failed or 4xx/5xx network requests since the last "
        "navigation. Check this after loading or interacting with the app.", {})),
    ("browser_eval", tool_browser_eval, _fn(
        "browser_eval",
        "Evaluate a JavaScript expression in the page and return its JSON result (the user confirms each one). "
        "Prefer snapshot/console; use this for state the page doesn't show (e.g. localStorage keys).",
        {"expression": {"type": "string"}}, ["expression"])),
    ("browser_close", tool_browser_close, _fn("browser_close", "Close the agent's browser.", {})),
]


def register_browser_tools() -> None:
    """Register the browser tools (idempotent); off unless capabilities.browser."""
    from ..small_model import APP_CONFIG as _cfg
    if not _cfg.get("capabilities", {}).get("browser", False):
        return
    for name, fn, schema in BROWSER_TOOLS:
        registry.register(name, fn, schema, source="browser", meta={"label": name.replace("_", " ")}, replace=True)
