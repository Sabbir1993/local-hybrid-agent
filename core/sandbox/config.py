"""Where the runner is and whether the chat may use it. Dev and production differ only here.

Address: the RUNNER_URL environment variable, else sandbox.runner_url in config/app.json. The token is never in a file: it sits
in the OS keychain (core/credentials.py), one entry per runner address, so re-pairing against production leaves dev alone."""
import os
from typing import Optional

from .. import credentials

_TOKEN_PREFIX = "sandbox-runner:"


def cfg() -> dict:
    from ..small_model import APP_CONFIG
    s = APP_CONFIG.get("sandbox")
    return s if isinstance(s, dict) else {}


def runner_url() -> str:
    return (os.environ.get("RUNNER_URL") or str(cfg().get("runner_url") or "")).strip().rstrip("/")


def fingerprint() -> str:
    return str(cfg().get("fingerprint") or "").strip().lower()


def allow_public() -> bool:
    return bool(cfg().get("allow_public_runner", False))


def enabled() -> bool:
    return bool(cfg().get("enabled", False))


def get_token(url: Optional[str] = None) -> Optional[str]:
    return credentials.get_token(_TOKEN_PREFIX + (url or runner_url()))


def set_token(url: str, token: str) -> None:
    credentials.set_token(_TOKEN_PREFIX + url, token)


def delete_token(url: Optional[str] = None) -> None:
    credentials.delete_token(_TOKEN_PREFIX + (url or runner_url()))


def save(**fields) -> None:
    """Write sandbox.* to config/app.json (and the live config)."""
    from ..config import update_app_config
    from ..small_model import APP_CONFIG

    def _apply(c):
        c.setdefault("sandbox", {}).update(fields)
    update_app_config(_apply)
    APP_CONFIG.setdefault("sandbox", {}).update(fields)


def configured() -> bool:
    """Everything needed to talk to the runner is present. The chat uses it only when this AND enabled() AND a live check pass."""
    return bool(runner_url() and fingerprint() and get_token())
