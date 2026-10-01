"""core/totp.py - TOTP second factor (RFC 6238, SHA-1) on the standard library.

No new dependency: the codebase already vendors `cryptography`, but TOTP
verification is HMAC-SHA1 + base32 + struct -- pulling pyotp in for that would
grow the lockfile and the audit surface for nothing. Secrets are 160-bit
(secrets.token_bytes), codes are 6 digits on a 30-second step, verification
accepts +-1 step for clock skew and refuses an already-used counter (replay
inside the window).
"""

import base64
import hashlib
import hmac
import secrets
import struct
import time

STEP_S = 30
DIGITS = 6
VERIFY_WINDOW = 1
SECRET_BYTES = 20


def generate_secret(nbytes: int = SECRET_BYTES) -> str:
    """Fresh base32 secret (no padding) for one enrollment."""
    return base64.b32encode(secrets.token_bytes(nbytes)).decode().rstrip("=")


def otpauth_uri(secret: str, username: str, issuer: str = "A770") -> str:
    """otpauth:// URI the user scans or types into their authenticator app."""
    user = str(username or "").strip() or "user"
    return f"otpauth://totp/{issuer}:{user}?secret={secret}&issuer={issuer}&digits={DIGITS}&period={STEP_S}"


def _counter(at: float) -> int:
    return int(at // STEP_S)


def code_at(secret: str, at: float) -> str:
    """The code valid at Unix time `at` (used by tests; production passes now)."""
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    msg = struct.pack(">Q", _counter(at))
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** DIGITS)).zfill(DIGITS)


def verify(secret: str, code: str, now: float = None, window: int = VERIFY_WINDOW,
           last_counter=None) -> "int | None":
    """The matched time-counter, or None. `last_counter` is the counter of the
    last accepted code: a code at or below it is a replay and is refused even
    when it is otherwise valid. Comparisons are constant-time."""
    if not secret or not code:
        return None
    at = time.time() if now is None else now
    center = _counter(at)
    for c in range(center - window, center + 1 + window):
        if c < 0:
            continue
        if last_counter is not None and c <= last_counter:
            continue
        if hmac.compare_digest(code_at(secret, c * STEP_S), str(code).strip()):
            return c
    return None


def generate_backup_codes(n: int = 8) -> "list[str]":
    """Single-use recovery codes (`xxxx-xxxx`); only sha256 hashes are stored."""
    return [f"{secrets.token_hex(2)}-{secrets.token_hex(2)}" for _ in range(max(1, int(n or 0)))]
