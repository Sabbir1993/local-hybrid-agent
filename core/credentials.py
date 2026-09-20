"""OS-keychain-backed token storage for MCP connector authorization.

Tokens obtained via OAuth device flow (see core/oauth_device_flow.py) are stored here,
never in config/app.json or any other plaintext file on disk.
"""

from typing import Optional

_SERVICE = "a770-dual-runtime-mcp"


def _keyring():
    import keyring  # local import: keep this an optional dependency at module load
    return keyring


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
