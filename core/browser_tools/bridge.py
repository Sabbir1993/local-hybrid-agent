import base64
import io
import sys
import time
from typing import Optional

from .. import companion_bridge
from .constants import MAX_RESULT_CHARS, SCREEN_DIR, UPGRADE_HINT

_thumbs: dict = {}      # (user id, tool call key) -> thumbnail data URL, popped by the SSE layer


def _mask(text: str) -> str:
    from ..pan import mask_pans
    return mask_pans(text)[0] if isinstance(text, str) else text


def _clip(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n... (truncated, {len(text)} chars)"


def _device():
    """(user id, workspace Path) or raises WorkspaceAccessDenied."""
    from ..agent_tools import require_device_workspace
    return require_device_workspace()


async def call_companion(op: str, params: dict, timeout: float = 90) -> dict:
    uid, _ = _device()
    cb = getattr(sys.modules.get("core.browser_tools"), "companion_bridge", companion_bridge)
    try:
        return await cb.call(uid, op, params, timeout=timeout)
    except RuntimeError as e:
        if str(e).startswith("unknown op"):
            raise RuntimeError(UPGRADE_HINT)
        raise


def _session_id() -> str:
    """One browser context per chat session (falls back to a per-user default)."""
    from ..agent_tools import get_plan_context
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
    from ..request_context import get_current_user_id
    # keyed by (user, tool): the args the tool saw may have been repaired in between
    return _thumbs.pop((get_current_user_id(), name), None)


async def handle_screenshot(tool_name: str, args: dict, png_b64: str, label: str, question: str) -> str:
    """Save on the device, stash a UI thumbnail, and describe it with a (local) vision model."""
    from ..request_context import get_current_user_id
    from ..small_model import APP_CONFIG, describe_image_bytes
    uid, ws = _device()
    png = base64.b64decode(png_b64 or "")
    if not png:
        return "error: screenshot was empty"
    lines = []
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in label)[:40] or "screen"
    rel = f"{SCREEN_DIR}/{time.strftime('%Y%m%d-%H%M%S')}-{safe}.png"
    cb = getattr(sys.modules.get("core.browser_tools"), "companion_bridge", companion_bridge)
    try:
        await cb.call(uid, "fs.write_b64", {"path": str(ws / rel), "data": png_b64}, timeout=30)
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


def _base(args: dict) -> dict:
    return {"session": _session_id()}


def _target(args: dict) -> dict:
    return {k: args[k] for k in ("ref", "selector", "text") if args.get(k)}
