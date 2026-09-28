"""Agent browser tools: drive a real browser on the user's machine to look at what
the agent built (like Claude's browser / Playwright MCP loop).

Everything runs on the user's device through the companion ("browser.*" ops in
companion/browserops.js), never on this server -- same device-only rule as the file
tools (core.agent_tools.require_device_workspace). The companion owns the safety
policy: throwaway browser profile, local dev hosts open freely, any other site
needs a local Allow, no typing into password / card fields, page JS confirmed.

What reaches the model is text: the page's accessibility snapshot (elements carry
[ref=eN] handles for click/type), console errors, failed requests, and -- for
screenshots -- a description from a LOCAL vision model (capabilities.
screenshot_cloud_vision opts into a cloud one). All of it is PAN-masked first.
Screenshots are saved into the project on the device (.agent/screens/) and a small
thumbnail is shown in the run view; neither is stored on this server.

Enabled by capabilities.browser in config/app.json.
"""

import base64
import io
import sys
import time
from typing import Optional

from . import companion_bridge
from .registry import registry

MAX_RESULT_CHARS = 24000
SCREEN_DIR = ".agent/screens"
_thumbs: dict = {}      # (user id, tool call key) -> thumbnail data URL, popped by the SSE layer

UPGRADE_HINT = ("the A770 Companion on this machine is too old for this tool - "
                "update it (v0.2.0 or newer) and reconnect")


def _mask(text: str) -> str:
    from .pan import mask_pans
    return mask_pans(text)[0] if isinstance(text, str) else text


def _clip(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n... (truncated, {len(text)} chars)"


def _device():
    """(user id, workspace Path) or raises WorkspaceAccessDenied."""
    from .agent_tools import require_device_workspace
    return require_device_workspace()


async def call_companion(op: str, params: dict, timeout: float = 90) -> dict:
    uid, _ = _device()
    try:
        return await companion_bridge.call(uid, op, params, timeout=timeout)
    except RuntimeError as e:
        if str(e).startswith("unknown op"):
            raise RuntimeError(UPGRADE_HINT)
        raise


def _session_id() -> str:
    """One browser context per chat session (falls back to a per-user default)."""
    from .agent_tools import get_plan_context
    sid = get_plan_context()
    return f"s{sid}" if sid else "default"


def _thumbnail(png: bytes) -> Optional[str]:
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(png)).convert("RGB")
        im.thumbnail((720, 720))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=70)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        print(f"[browser] thumbnail failed: {type(e).__name__}", file=sys.stderr)
        return None


def pop_thumbnail(name: str, args: dict) -> Optional[str]:
    from .request_context import get_current_user_id
    # keyed by (user, tool): the args the tool saw may have been repaired in between
    return _thumbs.pop((get_current_user_id(), name), None)


async def handle_screenshot(tool_name: str, args: dict, png_b64: str, label: str, question: str) -> str:
    """Save on the device, stash a UI thumbnail, and describe it with a (local) vision model."""
    from .request_context import get_current_user_id
    from .small_model import APP_CONFIG, describe_image_bytes
    uid, ws = _device()
    png = base64.b64decode(png_b64 or "")
    if not png:
        return "error: screenshot was empty"
    lines = []
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in label)[:40] or "screen"
    rel = f"{SCREEN_DIR}/{time.strftime('%Y%m%d-%H%M%S')}-{safe}.png"
    try:
        await companion_bridge.call(uid, "fs.write_b64", {"path": str(ws / rel), "data": png_b64}, timeout=30)
        lines.append(f"saved: {rel} ({len(png)} bytes)")
    except Exception as e:
        lines.append(f"(not saved to the project: {e})")
    thumb = _thumbnail(png)
    if thumb:
        _thumbs[(get_current_user_id(), tool_name)] = thumb
    if args.get("describe", True):
        cloud_ok = bool(APP_CONFIG.get("capabilities", {}).get("screenshot_cloud_vision", False))
        desc = await describe_image_bytes(png, question, force_local=not cloud_ok)
        lines.append("what the screen shows (vision model):\n" + _mask(desc))
    return "\n".join(lines)


def _page_text(d: dict, extra: str = "") -> str:
    head = [f"url: {d.get('url', '')}", f"title: {d.get('title', '')}"]
    if d.get("device") and d["device"] != "desktop":
        head.append(f"device: {d['device']}")
    if d.get("status"):
        head.append(f"http status: {d['status']}")
    if d.get("error"):
        head.append(f"error: {d['error']}")
    out = "\n".join(head)
    if extra:
        out += "\n" + extra
    if d.get("snapshot"):
        out += "\n\npage snapshot (use ref=eN with browser_click / browser_type):\n" + d["snapshot"]
    return _clip(_mask(out))


def _err(e: Exception) -> str:
    return f"error: {e}"


# ---------------- tools ----------------

def _base(args: dict) -> dict:
    return {"session": _session_id()}


def _target(args: dict) -> dict:
    return {k: args[k] for k in ("ref", "selector", "text") if args.get(k)}


async def tool_browser_navigate(args: dict) -> str:
    url = str(args.get("url") or "").strip()
    if not url:
        return "error: url required"
    try:
        d = await call_companion("browser.navigate", {
            **_base(args), "url": url, "device": args.get("device"),
            "headless": not bool(args.get("show")), "timeout_ms": 30000}, timeout=120)
    except Exception as e:
        return _err(e)
    return _page_text(d)


async def tool_browser_snapshot(args: dict) -> str:
    try:
        return _page_text(await call_companion("browser.snapshot", _base(args)))
    except Exception as e:
        return _err(e)


async def tool_browser_click(args: dict) -> str:
    if not _target(args):
        return "error: give ref (from the snapshot), selector or text"
    try:
        d = await call_companion("browser.click", {**_base(args), **_target(args),
                                                   "double": bool(args.get("double")),
                                                   "button": args.get("button")})
    except Exception as e:
        return _err(e)
    return _page_text(d)


async def tool_browser_type(args: dict) -> str:
    if not _target(args):
        return "error: give ref (from the snapshot), selector or text"
    try:
        d = await call_companion("browser.type", {**_base(args), **_target(args),
                                                  "text_value": str(args.get("value", "")),
                                                  "submit": bool(args.get("submit")),
                                                  "clear": args.get("clear", True)})
    except Exception as e:
        return _err(e)
    return _page_text(d)


async def tool_browser_press(args: dict) -> str:
    try:
        return _page_text(await call_companion("browser.press", {**_base(args), "key": args.get("key") or "Enter"}))
    except Exception as e:
        return _err(e)


async def tool_browser_select(args: dict) -> str:
    try:
        return _page_text(await call_companion("browser.select", {**_base(args), **_target(args),
                                                                  "values": args.get("values")}))
    except Exception as e:
        return _err(e)


async def tool_browser_wait(args: dict) -> str:
    try:
        return _page_text(await call_companion("browser.wait", {**_base(args), "text": args.get("text"),
                                                                "timeout_ms": args.get("timeout_ms")}))
    except Exception as e:
        return _err(e)


async def tool_browser_screenshot(args: dict) -> str:
    try:
        d = await call_companion("browser.screenshot", {**_base(args), "full_page": bool(args.get("full_page"))},
                                 timeout=60)
    except Exception as e:
        return _err(e)
    q = args.get("question") or ("Describe this web page screenshot for a developer checking their app: "
                                 "layout, visible text, anything broken, overlapping, cut off or unstyled.")
    shot = await handle_screenshot("browser_screenshot", args, d.get("png_b64", ""), "browser", q)
    return _clip(_mask(f"url: {d.get('url', '')}\ntitle: {d.get('title', '')}\n{shot}"))


async def tool_browser_console(args: dict) -> str:
    try:
        d = await call_companion("browser.console", _base(args))
    except Exception as e:
        return _err(e)
    con = d.get("console") or []
    net = d.get("network") or []
    lines = [f"url: {d.get('url', '')}", f"console errors/warnings: {len(con)}"]
    lines += [f"  [{c.get('type')}] {c.get('text')}" for c in con[-60:]]
    lines.append(f"failed / 4xx-5xx requests: {len(net)}")
    lines += [f"  {n.get('method', '')} {n.get('url')} -> {n.get('status') or n.get('failure')}" for n in net[-60:]]
    if not con and not net:
        lines.append("(no errors since the last navigation)")
    return _clip(_mask("\n".join(lines)))


async def tool_browser_eval(args: dict) -> str:
    expr = str(args.get("expression") or "").strip()
    if not expr:
        return "error: expression required"
    try:
        d = await call_companion("browser.eval", {**_base(args), "expression": expr}, timeout=180)
    except Exception as e:
        return _err(e)
    return _clip(_mask(f"url: {d.get('url', '')}\nresult:\n{d.get('result', '')}"))


async def tool_browser_close(args: dict) -> str:
    try:
        d = await call_companion("browser.close", _base(args))
    except Exception as e:
        return _err(e)
    return "browser closed" if d.get("closed") else "no browser was open"


def _fn(name, desc, props, required=()):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": list(required)}}}


_TARGET_PROPS = {
    "ref": {"type": "string", "description": "element ref from the latest snapshot, e.g. e12"},
    "selector": {"type": "string", "description": "CSS selector (when there is no ref)"},
    "text": {"type": "string", "description": "visible text of the element (last resort)"},
}

DEVICES = ["desktop", "iphone-14", "iphone-se", "pixel-7", "galaxy-s9", "ipad"]

BROWSER_TOOLS = [
    ("browser_navigate", tool_browser_navigate, _fn(
        "browser_navigate",
        "Open a URL in a real browser on the user's machine and return the page's accessibility snapshot "
        "(roles, names, [ref=eN] handles). Use it to check a web app you built or changed: start its dev server "
        "with run_shell first, then open http://localhost:<port>. Local dev hosts open directly; other sites ask "
        "the user. 'device' emulates a phone/tablet viewport (mobile web / responsive testing).",
        {"url": {"type": "string", "description": "e.g. http://localhost:5173/login"},
         "device": {"type": "string", "enum": DEVICES, "description": "viewport preset (default desktop)"},
         "show": {"type": "boolean", "description": "open a visible window the user can watch (default headless)"}},
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

READ_ONLY_BROWSER_TOOLS = {"browser_snapshot", "browser_console", "browser_screenshot"}


def register_browser_tools() -> None:
    """Register the browser tools (idempotent); off unless capabilities.browser."""
    from .small_model import APP_CONFIG as _cfg
    if not _cfg.get("capabilities", {}).get("browser", False):
        return
    for name, fn, schema in BROWSER_TOOLS:
        registry.register(name, fn, schema, source="browser", meta={"label": name.replace("_", " ")}, replace=True)
