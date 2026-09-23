"""Encoding helpers: base64 / url / hex, and sha256 / hmac-sha256 digests.

HMAC keys passed to digest are used in-memory only and never echoed back.
"""
import base64
import binascii
import hashlib
import hmac
import urllib.parse

MAX_IN = 100_000
_SCHEMES = ["base64", "base64url", "url", "hex"]


def register(api):
    params = {"type": "object",
              "properties": {"text": {"type": "string"},
                             "scheme": {"type": "string", "enum": _SCHEMES}},
              "required": ["text", "scheme"]}
    api.add_tool("encode", _encode, "Encode text as base64, base64url, url or hex.", params)
    api.add_tool("decode", _decode, "Decode base64, base64url, url or hex text.", params)
    api.add_tool(
        "digest",
        _digest,
        "SHA-256 hex digest of text, or HMAC-SHA256 when a key is given (key is never echoed).",
        {"type": "object",
         "properties": {"text": {"type": "string"},
                        "key": {"type": "string", "description": "optional HMAC key"}},
         "required": ["text"]},
    )


def _text(args):
    return str(args.get("text") or "")[:MAX_IN]


def _encode(args: dict) -> str:
    raw = _text(args).encode("utf-8")
    scheme = args.get("scheme")
    if scheme == "base64":
        return base64.b64encode(raw).decode()
    if scheme == "base64url":
        return base64.urlsafe_b64encode(raw).decode()
    if scheme == "url":
        return urllib.parse.quote(_text(args), safe="")
    if scheme == "hex":
        return raw.hex()
    return f"error: scheme must be one of {', '.join(_SCHEMES)}"


def _decode(args: dict) -> str:
    s = _text(args).strip()
    scheme = args.get("scheme")
    try:
        if scheme in ("base64", "base64url"):
            s += "=" * (-len(s) % 4)
            fn = base64.urlsafe_b64decode if scheme == "base64url" else base64.b64decode
            out = fn(s)
        elif scheme == "url":
            return urllib.parse.unquote(s)
        elif scheme == "hex":
            out = bytes.fromhex(s)
        else:
            return f"error: scheme must be one of {', '.join(_SCHEMES)}"
    except (binascii.Error, ValueError) as e:
        return f"error: not valid {scheme}: {e}"
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        return f"(binary, {len(out)} bytes) hex: {out.hex()[:2000]}"


def _digest(args: dict) -> str:
    data = _text(args).encode("utf-8")
    key = args.get("key")
    if key:
        return "hmac-sha256: " + hmac.new(str(key).encode("utf-8"), data, hashlib.sha256).hexdigest()
    return "sha256: " + hashlib.sha256(data).hexdigest()
