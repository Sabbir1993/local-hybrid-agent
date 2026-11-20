"""Config block `speech_corpus` in app.json, the corpus folder rules, the on/off switch and the shared access code.

  enabled             the public /voice page and its API answer only when true (404 otherwise)
  dir                 absolute folder OUTSIDE the app folder: corpus.db + audio/ go here
  access_code_sha256  hash of the code carried in the shared link (/voice?k=<code>); the code itself is never stored
  lines_per_session   lines handed to one session (default 15, 5..30)
  max_clip_seconds    longest accepted clip (default 20, 5..30)
  sessions_per_day    sessions one employee ID may start per 24 h (default 5)
"""
import hashlib
import hmac
import os
import secrets
from pathlib import Path

from ..config import BASE_DIR

DEFAULTS = {
    "enabled": False,
    "dir": "",
    "access_code_sha256": "",
    "lines_per_session": 15,
    "max_clip_seconds": 20,
    "sessions_per_day": 5,
}


class CorpusConfigError(Exception):
    """The corpus cannot be used as configured; the message says why, in plain words."""


def cfg() -> dict:
    from ..small_model import APP_CONFIG
    block = APP_CONFIG.get("speech_corpus")
    return {**DEFAULTS, **(block if isinstance(block, dict) else {})}


def _int(key: str, lo: int, hi: int) -> int:
    try:
        v = int(cfg().get(key, DEFAULTS[key]))
    except (TypeError, ValueError):
        v = DEFAULTS[key]
    return max(lo, min(hi, v))


def lines_per_session() -> int:
    return _int("lines_per_session", 5, 30)


def max_clip_seconds() -> int:
    return _int("max_clip_seconds", 5, 30)


def sessions_per_day() -> int:
    return _int("sessions_per_day", 1, 50)


def corpus_dir(app_dir: Path = None) -> Path:
    """The configured folder, checked: absolute, outside the app folder, existing (created if only it is missing) and
    writable. Raises CorpusConfigError with the reason."""
    raw = str(cfg().get("dir") or "").strip()
    if not raw:
        raise CorpusConfigError("No corpus folder is set (speech_corpus.dir in config/app.json).")
    p = Path(raw).expanduser()
    if not p.is_absolute():
        raise CorpusConfigError("The corpus folder must be an absolute path.")
    p = p.resolve()
    app = Path(app_dir or BASE_DIR).resolve()
    if p == app or p.is_relative_to(app):
        raise CorpusConfigError("The corpus folder must be outside the application folder.")
    if not p.exists():
        if not p.parent.exists():
            raise CorpusConfigError(f"The corpus folder's parent does not exist: {p.parent}")
        try:
            p.mkdir(mode=0o700)
        except OSError as e:
            raise CorpusConfigError(f"Could not create the corpus folder: {e.strerror or e}") from None
    if not p.is_dir():
        raise CorpusConfigError("The corpus folder path is not a folder.")
    if not os.access(p, os.W_OK):
        raise CorpusConfigError("The app cannot write to the corpus folder.")
    return p


def has_code() -> bool:
    return bool(str(cfg().get("access_code_sha256") or "").strip())


def _hash(code: str) -> str:
    return hashlib.sha256(code.strip().encode("utf-8")).hexdigest()


def code_ok(code: str) -> bool:
    want = str(cfg().get("access_code_sha256") or "").strip().lower()
    if not want or not code:
        return False
    return hmac.compare_digest(_hash(str(code)), want)


def _save(patch: dict) -> dict:
    """Write keys into the speech_corpus block of app.json and the live config."""
    from ..config import update_app_config
    from ..small_model import APP_CONFIG

    def mut(c):
        block = c.get("speech_corpus") if isinstance(c.get("speech_corpus"), dict) else {}
        block.update(patch)
        c["speech_corpus"] = block
    saved = update_app_config(mut)
    APP_CONFIG["speech_corpus"] = dict(saved.get("speech_corpus") or {})
    return cfg()


_KEYCHAIN_ID = "speech-corpus-link-code"


def rotate_code() -> str:
    """New random code. app.json keeps only its hash (that is what the page is checked against); the code itself goes
    to the OS keychain so admins can see the current link again later. The old link stops working at once."""
    code = secrets.token_urlsafe(9)
    _save({"access_code_sha256": _hash(code)})
    try:
        from .. import credentials
        credentials.set_token(_KEYCHAIN_ID, code)
    except Exception:
        pass        # no usable keychain: the link still works, it just can't be shown again (current_code -> None)
    return code


def current_code():
    """The code of the current link from the keychain, or None (keychain unavailable, or the stored code is not the
    one app.json was last set to - e.g. config copied from another server)."""
    if not has_code():
        return None
    try:
        from .. import credentials
        code = credentials.get_token(_KEYCHAIN_ID)
    except Exception:
        return None
    return code if code and code_ok(code) else None


def set_enabled(on: bool) -> dict:
    """Turn the public page on or off. Turning on refuses without a code or a usable folder."""
    if on:
        if not has_code():
            raise CorpusConfigError("Create an access code first, then turn collection on.")
        corpus_dir()
    return _save({"enabled": bool(on)})


def is_enabled() -> bool:
    return bool(cfg().get("enabled"))
