import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Optional

from ..config import CONFIG_FILE, PROVIDERS_DIR, atomic_write_json

_KEY_REF = "keyring"


def _get_providers_dir() -> Path:
    mod = sys.modules.get("core.cloud")
    return getattr(mod, "PROVIDERS_DIR", PROVIDERS_DIR) if mod else PROVIDERS_DIR


def _get_config_file() -> Path:
    mod = sys.modules.get("core.cloud")
    return getattr(mod, "CONFIG_FILE", CONFIG_FILE) if mod else CONFIG_FILE


def check_url_wrapper(url: str) -> str:
    mod = sys.modules.get("core.cloud")
    fn = getattr(mod, "check_provider_url", None) if mod else None
    if fn is not None:
        return fn(url)
    from .client import check_provider_url
    return check_provider_url(url)


def _provider_file(user_id: int) -> Path:
    return _get_providers_dir() / f"user_{user_id}.json"


def _read_json(p: Path) -> dict:
    try:
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                return d
    except Exception as e:
        print(f"[cloud] {p.name} unreadable: {e}")
    return {}


def _key_id(user_id: int, provider: str) -> str:
    return f"cloud:user_{user_id}:{provider}"


def _hydrate_keys(user_id: int, cfg: dict) -> dict:
    """In-memory only: fill options.apiKey from the keychain; migrate plaintext keys."""
    from .. import credentials
    plaintext = False
    for name, entry in (cfg.get("provider") or {}).items():
        opts = entry.get("options") if isinstance(entry, dict) else None
        if not isinstance(opts, dict):
            continue
        if opts.get("apiKeyRef") == _KEY_REF:
            opts["apiKey"] = credentials.get_token(_key_id(user_id, name)) or ""
        elif str(opts.get("apiKey") or opts.get("api_key") or "").strip():
            plaintext = True
    if plaintext:
        _write_providers(user_id, cfg)
    return cfg


def _write_providers(user_id: int, cfg: dict) -> None:
    """Persist a user's providers with API keys moved to the OS keychain."""
    from .. import credentials
    out = deepcopy(cfg)
    for name, entry in (out.get("provider") or {}).items():
        opts = entry.get("options") if isinstance(entry, dict) else None
        if not isinstance(opts, dict):
            continue
        key = str(opts.pop("apiKey", "") or opts.pop("api_key", "") or "").strip()
        opts.pop("api_key", None)
        if key:
            try:
                credentials.set_token(_key_id(user_id, name), key)
                opts["apiKeyRef"] = _KEY_REF
            except Exception as e:
                print(f"[cloud] keychain unavailable, API key for '{name}' kept in file: {e}",
                      file=sys.stderr)
                opts["apiKey"] = key
    _get_providers_dir().mkdir(parents=True, exist_ok=True)
    atomic_write_json(_provider_file(user_id), out)
