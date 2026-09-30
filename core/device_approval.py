"""In-app approval for actions that the companion would otherwise confirm in a native OS dialog.

Running page JavaScript in the agent's browser, opening a non-local website, and starting / installing on a device
used to pop a dialog from the companion app, which looks unlike the web app, can hide behind other windows, and
leaves the chat on "Working..." while it waits. The same question is now asked in the web app's permission card
(the one shell commands use); the answer travels to the companion as `approved_in_app`
(core.request_context.device_approved -> core.companion_bridge.call), and the companion skips its own dialog.
An older companion ignores the flag and still shows its dialog, so nothing gets less safe.
"""

from typing import Optional
from urllib.parse import urlparse

KIND_EVAL = "browser_eval"
KIND_OPEN = "browser_open"
KIND_DEVICE = "device"
KINDS = (KIND_EVAL, KIND_OPEN, KIND_DEVICE)


def is_local_host(host: str) -> bool:
    """Same rule as companion/browserops.js isLocalHost: dev servers need no question."""
    h = (host or "").strip("[]").lower()
    return h in ("localhost", "127.0.0.1", "::1") or h.endswith(".localhost") or h.endswith(".test")


def origin_of(url: str) -> str:
    try:
        u = urlparse(url)
    except ValueError:
        return ""
    return f"{u.scheme}://{u.netloc}" if u.scheme in ("http", "https") and u.hostname else ""


def request_for(name: str, args: Optional[dict]) -> Optional[dict]:
    """{kind, text, key} describing what the user is asked to allow, or None when this call needs no question.
    `key` identifies "the same thing" for the allow-for-this-run memory (an origin, a device action kind)."""
    args = args or {}
    if name == "browser_eval":
        expr = str(args.get("expression") or "").strip()
        if not expr:
            return None
        return {"kind": KIND_EVAL, "key": KIND_EVAL,
                "text": f"{expr}\n\nruns as JavaScript inside the agent's test browser (a fresh profile: none of your logins)."}
    if name == "browser_navigate":
        url = str(args.get("url") or "").strip()
        origin = origin_of(url)
        if not origin or is_local_host(urlparse(url).hostname or ""):
            return None
        return {"kind": KIND_OPEN, "key": origin,
                "text": f"{origin}\n\nThe agent's browser is a fresh profile (none of your logins or cookies), but anything "
                        "the page shows is sent to the AI. Allow only sites you are testing."}
    if name == "mobile_boot":
        what = args.get("avd") or args.get("udid") or args.get("device") or "the default device"
        return {"kind": KIND_DEVICE, "key": KIND_DEVICE, "text": f"Start the emulator / simulator: {what}"}
    if name == "mobile_install":
        return {"kind": KIND_DEVICE, "key": KIND_DEVICE, "text": f"Install this app on the device: {args.get('path') or ''}"}
    if name == "mobile_connect":
        bits = []
        if args.get("pair_host_port"):
            bits.append(f"pair with {args['pair_host_port']}")
        if args.get("host_port"):
            bits.append(f"connect to {args['host_port']} over Wi-Fi")
        if not bits:
            return None
        return {"kind": KIND_DEVICE, "key": KIND_DEVICE, "text": "Wireless debugging: " + " and ".join(bits)}
    return None
