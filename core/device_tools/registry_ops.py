from ..browser_tools import _fn
from ..registry import registry
from .constants import _DEV, _POS
from .tools import (
    tool_mobile_boot,
    tool_mobile_connect,
    tool_mobile_devices,
    tool_mobile_install,
    tool_mobile_launch,
    tool_mobile_logs,
    tool_mobile_screenshot,
    tool_mobile_swipe,
    tool_mobile_tap,
    tool_mobile_type,
    tool_mobile_ui,
)

DEVICE_TOOLS = [
    ("mobile_devices", tool_mobile_devices, _fn(
        "mobile_devices", "List connected Android phones/emulators, bootable Android emulators (AVDs) and iOS "
        "simulators on the user's machine. Start here when testing a mobile app.", {})),
    ("mobile_boot", tool_mobile_boot, _fn(
        "mobile_boot", "Boot an Android emulator (avd name) or an iOS simulator (device udid, platform=ios). "
        "The user confirms on their machine.",
        {**_DEV, "avd": {"type": "string"}, "cold": {"type": "boolean", "description": "cold boot (no snapshot)"},
         "headless": {"type": "boolean", "description": "no emulator window"}})),
    ("mobile_connect", tool_mobile_connect, _fn(
        "mobile_connect", "Connect a physical Android phone over Wi-Fi (Developer options > Wireless debugging): "
        "pair once with pair_host_port + the 6-digit pair_code, then connect host_port. USB phones need nothing.",
        {"host_port": {"type": "string"}, "pair_host_port": {"type": "string"}, "pair_code": {"type": "string"}})),
    ("mobile_install", tool_mobile_install, _fn(
        "mobile_install", "Install a built app from the project: .apk (Android, e.g. app/build/outputs/apk/debug/"
        "app-debug.apk) or simulator .app (iOS). The user confirms.",
        {**_DEV, "path": {"type": "string", "description": "path inside the project"}}, ["path"])),
    ("mobile_launch", tool_mobile_launch, _fn(
        "mobile_launch", "Launch (restart=true to force-stop first) or stop an installed app.",
        {**_DEV, "app_id": {"type": "string", "description": "Android package / iOS bundle id"},
         "activity": {"type": "string", "description": "Android: explicit activity, e.g. .MainActivity"},
         "restart": {"type": "boolean"}, "stop": {"type": "boolean"}}, ["app_id"])),
    ("mobile_ui", tool_mobile_ui, _fn(
        "mobile_ui", "Dump the on-screen UI as a list of elements (text, id, position, clickable/editable) with "
        "[nN] refs for mobile_tap / mobile_type.", {**_DEV})),
    ("mobile_tap", tool_mobile_tap, _fn(
        "mobile_tap", "Tap an element (ref from mobile_ui) or x,y screen coordinates.",
        {**_DEV, **_POS, "long": {"type": "boolean", "description": "long press"}})),
    ("mobile_type", tool_mobile_type, _fn(
        "mobile_type", "Type text into the focused field (or tap ref/x,y first). Card-number-like text is refused: "
        "ask the user to enter test card data.",
        {**_DEV, **_POS, "text": {"type": "string"}, "submit": {"type": "boolean"}}, ["text"])),
    ("mobile_swipe", tool_mobile_swipe, _fn(
        "mobile_swipe", "Android: swipe x1,y1 -> x2,y2 (scroll), or press a key (BACK, HOME, ENTER, APP_SWITCH).",
        {**_DEV, "x1": {"type": "integer"}, "y1": {"type": "integer"}, "x2": {"type": "integer"},
         "y2": {"type": "integer"}, "duration_ms": {"type": "integer"}, "key": {"type": "string"}})),
    ("mobile_screenshot", tool_mobile_screenshot, _fn(
        "mobile_screenshot", "Screenshot the device: saved to .agent/screens/, shown to the user, and described by "
        "a vision model.", {**_DEV, "question": {"type": "string"}, "describe": {"type": "boolean"}})),
    ("mobile_logs", tool_mobile_logs, _fn(
        "mobile_logs", "Recent device logs (Android logcat, filtered to app_id when given; crash=true for the crash "
        "buffer). Check after a crash or a failed action.",
        {**_DEV, "app_id": {"type": "string"}, "lines": {"type": "integer"}, "crash": {"type": "boolean"},
         "errors_only": {"type": "boolean"}, "minutes": {"type": "integer", "description": "iOS: how far back"}})),
]


def register_device_tools() -> None:
    """Register the mobile tools (idempotent); off unless capabilities.mobile."""
    from ..small_model import APP_CONFIG as _cfg
    if not _cfg.get("capabilities", {}).get("mobile", False):
        return
    for name, fn, schema in DEVICE_TOOLS:
        registry.register(name, fn, schema, source="mobile", meta={"label": name.replace("_", " ")}, replace=True)
