from ..browser_tools import _clip, _mask, call_companion, handle_screenshot
from .helpers import _dev, _err, _plat, _resolve_app_path


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
