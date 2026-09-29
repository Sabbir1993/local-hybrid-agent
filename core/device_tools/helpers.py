from .constants import PLATFORMS


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


def _resolve_app_path(p: str) -> str:
    from ..agent_tools import _ws_resolve
    return str(_ws_resolve(p))
