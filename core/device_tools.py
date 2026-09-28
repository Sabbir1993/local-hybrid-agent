"""Mobile device tools: Android emulators / phones (adb) and iOS simulators (macOS
companions) on the user's machine, so the agent can install, run and poke the app it
is building and read its logs.

Runs through the companion ("android.*" / "ios.*" ops in companion/androidops.js and
iosops.js) -- never on this server. The companion builds every adb/simctl argv
itself (no free-form shell) and asks the user before installing an app, booting an
emulator or pairing a phone. UI trees, logs and screenshot descriptions are PAN-
masked before they reach the model; screenshots are saved into the project on the
device (.agent/screens/) and shown as a thumbnail in the run view.

Enabled by capabilities.mobile in config/app.json.
"""

from .browser_tools import _clip, _fn, _mask, call_companion, handle_screenshot
from .registry import registry

PLATFORMS = ["android", "ios"]


def _plat(args: dict) -> str:
    p = str(args.get("platform") or "android").lower()
    return p if p in PLATFORMS else "android"


def _dev(args: dict) -> dict:
    """serial (android) / udid (ios) selector."""
    out = {}
    if args.get("device"):
        out["serial"] = out["udid"] = str(args["device"])
    return out


def _err(e: Exception) -> str:
    return f"error: {e}"


async def tool_mobile_devices(args: dict) -> str:
    lines = []
    try:
        d = await call_companion("android.devices", {})
        devs = d.get("devices") or []
        lines.append("Android devices (adb):" if devs else "Android devices (adb): none connected")
        lines += [f"  {x['serial']}  {x['state']}  {x.get('model') or ''}{'  (emulator)' if x.get('emulator') else ''}"
                  for x in devs]
        lines.append("Android emulators (AVDs) you can boot with mobile_boot: " + (", ".join(d.get("avds") or []) or "none"))
        if d.get("adb_error"):
            lines.append(f"  adb problem: {d['adb_error']}")
        if not d.get("emulator"):
            lines.append("  (Android emulator not installed - Android Studio > SDK Manager > Android Emulator)")
    except Exception as e:
        lines.append(f"Android: {e}")
    try:
        d = await call_companion("ios.devices", {})
        sims = d.get("simulators") or []
        lines.append("iOS simulators:")
        lines += [f"  {s['udid']}  {s['state']}  {s['name']} ({s.get('runtime', '')})" for s in sims[:40]]
        if not d.get("idb"):
            lines.append("  (idb not installed: UI tree/taps unavailable, screenshots/logs work)")
    except Exception as e:
        if "need a Mac" not in str(e) and "Mac with Xcode" not in str(e):
            lines.append(f"iOS: {e}")
        else:
            lines.append("iOS: needs the companion running on a Mac with Xcode")
    return "\n".join(lines)


async def tool_mobile_boot(args: dict) -> str:
    try:
        if _plat(args) == "ios":
            d = await call_companion("ios.boot", _dev(args), timeout=300)
            return f"simulator {d.get('udid')} booted"
        name = str(args.get("avd") or "").strip()
        if not name:
            return "error: avd is required (see mobile_devices for the list)"
        d = await call_companion("android.boot_avd", {"avd": name, "cold": bool(args.get("cold")),
                                                      "headless": bool(args.get("headless"))}, timeout=330)
    except Exception as e:
        return _err(e)
    if d.get("booted"):
        return f"emulator '{d.get('avd')}' is up as {d.get('serial')}"
    return f"emulator '{d.get('avd')}' started{(' as ' + d['serial']) if d.get('serial') else ''} but not fully booted yet. {d.get('note', '')}"


async def tool_mobile_connect(args: dict) -> str:
    """Wireless debugging: pair (6-digit code) and/or connect host:port."""
    try:
        out = []
        if args.get("pair_host_port") and args.get("pair_code"):
            d = await call_companion("android.pair", {"host_port": args["pair_host_port"], "code": str(args["pair_code"])},
                                     timeout=60)
            out.append(d.get("output", ""))
        if args.get("host_port"):
            d = await call_companion("android.connect", {"host_port": args["host_port"]}, timeout=60)
            out.append(d.get("output", ""))
        return "\n".join(o for o in out if o) or "error: give host_port (and pair_host_port + pair_code to pair first)"
    except Exception as e:
        return _err(e)


def _resolve_app_path(p: str) -> str:
    from .agent_tools import _ws_resolve
    return str(_ws_resolve(p))


async def tool_mobile_install(args: dict) -> str:
    path = str(args.get("path") or "").strip()
    if not path:
        return "error: path to the .apk (Android) or .app (iOS simulator) is required"
    try:
        full = _resolve_app_path(path)
        if _plat(args) == "ios":
            d = await call_companion("ios.install", {**_dev(args), "app": full}, timeout=300)
            return f"installed {path} on {d.get('udid')}"
        d = await call_companion("android.install", {**_dev(args), "apk": full}, timeout=300)
    except Exception as e:
        return _err(e)
    return f"installed on {d.get('serial')}:\n{d.get('output', '')}"


async def tool_mobile_launch(args: dict) -> str:
    app = str(args.get("app_id") or "").strip()
    if not app:
        return "error: app_id (Android package / iOS bundle id) is required"
    try:
        if args.get("stop"):
            op = "android.stop" if _plat(args) == "android" else None
            if not op:
                return "error: stop is Android-only; relaunch with restart=true on iOS"
            d = await call_companion(op, {**_dev(args), "package": app})
            return f"stopped {app}"
        if _plat(args) == "ios":
            d = await call_companion("ios.launch", {**_dev(args), "bundle_id": app, "stop_first": bool(args.get("restart"))})
        else:
            d = await call_companion("android.launch", {**_dev(args), "package": app, "activity": args.get("activity"),
                                                        "stop_first": bool(args.get("restart"))})
    except Exception as e:
        return _err(e)
    return _mask(f"launched {app}\n{d.get('output', '')}")


async def tool_mobile_ui(args: dict) -> str:
    try:
        op = "ios.ui_dump" if _plat(args) == "ios" else "android.ui_dump"
        d = await call_companion(op, _dev(args), timeout=60)
    except Exception as e:
        return _err(e)
    head = f"device: {d.get('serial') or d.get('udid')}"
    if d.get("foreground_package"):
        head += f"\nforeground app: {d['foreground_package']}"
    body = d.get("tree") or "(no visible elements)"
    return _clip(_mask(f"{head}\nelements ({d.get('elements', 0)}; tap with ref=nN):\n{body}"))


async def tool_mobile_tap(args: dict) -> str:
    try:
        op = "ios.tap" if _plat(args) == "ios" else "android.tap"
        p = {**_dev(args), **{k: args[k] for k in ("ref", "x", "y") if args.get(k) is not None},
             "long": bool(args.get("long"))}
        d = await call_companion(op, p)
    except Exception as e:
        return _err(e)
    t = d.get("tapped") or {}
    return f"tapped at ({t.get('x')},{t.get('y')}) - call mobile_ui or mobile_screenshot to see the result"


async def tool_mobile_type(args: dict) -> str:
    try:
        op = "ios.text" if _plat(args) == "ios" else "android.text"
        p = {**_dev(args), "text": str(args.get("text") or ""), "submit": bool(args.get("submit")),
             **{k: args[k] for k in ("ref", "x", "y") if args.get(k) is not None}}
        d = await call_companion(op, p)
    except Exception as e:
        return _err(e)
    return f"typed {d.get('typed_chars', 0)} characters"


async def tool_mobile_swipe(args: dict) -> str:
    if _plat(args) == "ios":
        return "error: swipe is Android-only for now"
    try:
        if args.get("key"):
            d = await call_companion("android.key", {**_dev(args), "key": args["key"]})
            return f"pressed {d.get('key')}"
        d = await call_companion("android.swipe", {**_dev(args), **{k: args.get(k) for k in ("x1", "y1", "x2", "y2")},
                                                   "duration_ms": args.get("duration_ms")})
    except Exception as e:
        return _err(e)
    return f"swiped {d.get('swiped')}"


async def tool_mobile_screenshot(args: dict) -> str:
    try:
        op = "ios.screenshot" if _plat(args) == "ios" else "android.screenshot"
        d = await call_companion(op, _dev(args), timeout=60)
    except Exception as e:
        return _err(e)
    q = args.get("question") or ("Describe this mobile app screenshot for a developer testing their app: "
                                 "screen content, layout issues, error dialogs, clipped or overlapping UI.")
    shot = await handle_screenshot("mobile_screenshot", args, d.get("png_b64", ""), _plat(args), q)
    return _clip(_mask(f"device: {d.get('serial') or d.get('udid')}\n{shot}"))


async def tool_mobile_logs(args: dict) -> str:
    try:
        if _plat(args) == "ios":
            d = await call_companion("ios.logcat", {**_dev(args), "bundle_id": args.get("app_id"),
                                                   "minutes": args.get("minutes")}, timeout=60)
        else:
            d = await call_companion("android.logcat", {**_dev(args), "package": args.get("app_id"),
                                                       "lines": args.get("lines"), "crash": bool(args.get("crash")),
                                                       "errors_only": bool(args.get("errors_only"))}, timeout=60)
    except Exception as e:
        return _err(e)
    return _clip(_mask(d.get("log") or "(no log lines)"))


_DEV = {"device": {"type": "string", "description": "serial / udid from mobile_devices (optional with one device)"},
        "platform": {"type": "string", "enum": PLATFORMS, "description": "default android"}}
_POS = {"ref": {"type": "string", "description": "element ref from mobile_ui, e.g. n7"},
        "x": {"type": "integer"}, "y": {"type": "integer"}}

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

READ_ONLY_DEVICE_TOOLS = {"mobile_devices", "mobile_ui", "mobile_screenshot", "mobile_logs"}


def register_device_tools() -> None:
    """Register the mobile tools (idempotent); off unless capabilities.mobile."""
    from .small_model import APP_CONFIG as _cfg
    if not _cfg.get("capabilities", {}).get("mobile", False):
        return
    for name, fn, schema in DEVICE_TOOLS:
        registry.register(name, fn, schema, source="mobile", meta={"label": name.replace("_", " ")}, replace=True)
