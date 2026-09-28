"""OS-keychain-backed token storage for MCP connector authorization.

Tokens obtained via OAuth device flow (see core/oauth_device_flow.py) are stored here,
never in config/app.json or any other plaintext file on disk.
"""

from typing import Optional

_SERVICE = "a770-dual-runtime-mcp"


def _keyring():
    import keyring  # local import: keep this an optional dependency at module load
    return keyring


# Backends that keep secrets in a plaintext / weakly-obfuscated file or drop them.
_INSECURE_BACKENDS = ("keyrings.alt", "keyring.backends.fail", "keyring.backends.null",
                      "keyring.backends.chainer")


def keyring_backend_warning() -> Optional[str]:
    """None when the active keyring backend is an OS keychain (Windows
    Credential Manager, macOS Keychain, Secret Service, KWallet), else a
    warning to log at startup. Linux needs gnome-keyring/KWallet unlocked
    in the server's D-Bus session."""
    try:
        backend = _keyring().get_keyring()
    except Exception as e:
        return f"keyring unavailable ({e}) - API keys and MCP tokens cannot be stored"
    mod = type(backend).__module__
    if mod.startswith(_INSECURE_BACKENDS):
        chained = [type(b).__module__ for b in getattr(backend, "backends", [])]
        if chained and not any(m.startswith(_INSECURE_BACKENDS) for m in chained):
            return None
        return (f"insecure keyring backend {type(backend).__name__} ({mod}) - API keys and "
                f"MCP tokens need an OS keychain; on Linux run gnome-keyring or KWallet, "
                f"do not use keyrings.alt")
    return None


def set_token(server_id: str, token: str) -> None:
    _keyring().set_password(_SERVICE, server_id, token)


def get_token(server_id: str) -> Optional[str]:
    try:
        return _keyring().get_password(_SERVICE, server_id)
    except Exception:
        return None


def delete_token(server_id: str) -> None:
    try:
        _keyring().delete_password(_SERVICE, server_id)
    except Exception:
        pass


def has_token(server_id: str) -> bool:
    return bool(get_token(server_id))
