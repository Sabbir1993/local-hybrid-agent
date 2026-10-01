"""core/auth_db/mfa.py - TOTP enrollment state + hashed backup codes.

Secrets live on the users row (NULL = never enrolled); only sha256 hashes of
backup codes are stored, mirroring the session/token discipline -- a DB read
alone must not yield a usable second factor. All destructive transitions
(disable, admin reset, re-enroll) wipe the secret AND the codes together.
"""

import hashlib
import time

from .common import db


def _hash_code(code: str) -> str:
    return hashlib.sha256(str(code or "").strip().encode("utf-8")).hexdigest()


def get_totp(user_id: int) -> dict:
    row = db().execute(
        "SELECT totp_secret, totp_enabled, totp_last_counter FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if not row:
        return {"secret": None, "enabled": False, "last_counter": 0}
    return {"secret": row["totp_secret"], "enabled": bool(row["totp_enabled"]),
            "last_counter": int(row["totp_last_counter"] or 0)}


def totp_enabled(user_id: int) -> bool:
    return get_totp(user_id)["enabled"]


def set_totp_secret(user_id: int, secret: str) -> None:
    """Stage a fresh secret (enabled stays off until the user proves the
    authenticator with a confirm code). Wipes codes from any earlier enrollment."""
    db().execute("UPDATE users SET totp_secret = ?, totp_enabled = 0, totp_last_counter = 0 WHERE id = ?",
                 (secret, user_id))
    db().execute("DELETE FROM user_totp_backup WHERE user_id = ?", (user_id,))
    db().commit()


def enable_totp(user_id: int) -> None:
    db().execute("UPDATE users SET totp_enabled = 1 WHERE id = ? AND totp_secret IS NOT NULL", (user_id,))
    db().commit()


def record_totp_counter(user_id: int, counter: int) -> None:
    db().execute("UPDATE users SET totp_last_counter = ? WHERE id = ? AND totp_last_counter < ?",
                 (counter, user_id, counter))
    db().commit()


def disable_totp(user_id: int) -> None:
    """Full wipe: secret, enabled flag, replay counter, and every backup code."""
    db().execute("UPDATE users SET totp_secret = NULL, totp_enabled = 0, totp_last_counter = 0 WHERE id = ?",
                 (user_id,))
    db().execute("DELETE FROM user_totp_backup WHERE user_id = ?", (user_id,))
    db().commit()


admin_reset_mfa = disable_totp


def store_backup_codes(user_id: int, codes) -> None:
    now = time.time()
    db().execute("DELETE FROM user_totp_backup WHERE user_id = ?", (user_id,))
    db().executemany(
        "INSERT INTO user_totp_backup (user_id, code_hash, created_at) VALUES (?, ?, ?)",
        [(user_id, _hash_code(c), now) for c in codes],
    )
    db().commit()


def consume_backup_code(user_id: int, code: str) -> bool:
    """True once: a used or unknown code is refused (constant-time compare)."""
    import hmac as _hmac
    want = _hash_code(code)
    rows = db().execute(
        "SELECT id, code_hash FROM user_totp_backup WHERE user_id = ? AND used_at IS NULL", (user_id,)
    ).fetchall()
    for r in rows:
        if _hmac.compare_digest(r["code_hash"], want):
            cur = db().execute("UPDATE user_totp_backup SET used_at = ? WHERE id = ? AND used_at IS NULL",
                               (time.time(), r["id"]))
            db().commit()
            return cur.rowcount == 1
    return False


def remaining_backup_codes(user_id: int) -> int:
    row = db().execute(
        "SELECT COUNT(*) c FROM user_totp_backup WHERE user_id = ? AND used_at IS NULL", (user_id,)
    ).fetchone()
    return int(row["c"]) if row else 0
