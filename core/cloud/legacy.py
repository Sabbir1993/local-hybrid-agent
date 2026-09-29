import os
from pathlib import Path

from .config import _KEY_REF, _hydrate_keys, _key_id, _provider_file, _read_json, _write_providers, reload


def _shred(path: Path) -> None:
    """Overwrite a file with zeros before deleting it."""
    try:
        n = path.stat().st_size
        with open(path, "r+b") as f:
            f.write(b"\0" * n)
            f.flush()
            os.fsync(f.fileno())
    finally:
        path.unlink()


def import_legacy_file(path: Path, user_id: int) -> dict:
    """Move plaintext API keys of legacy shared providers file into the OS keychain."""
    from .. import credentials
    path = Path(path)
    legacy = _read_json(path)
    own = _read_json(_provider_file(user_id))
    if not own:
        _write_providers(user_id, legacy)
        reload(user_id)
        _shred(path)
        n = sum(1 for e in (legacy.get("provider") or {}).values()
                if isinstance(e, dict) and (e.get("options") or {}).get("apiKey"))
        return {"imported": n, "skipped": 0, "removed": True}
    imported = skipped = 0
    for name, entry in (legacy.get("provider") or {}).items():
        opts = (entry or {}).get("options") if isinstance(entry, dict) else None
        key = str((opts or {}).get("apiKey") or (opts or {}).get("api_key") or "").strip()
        if not key:
            continue
        mine = (own.get("provider") or {}).get(name)
        if not isinstance(mine, dict) or credentials.has_token(_key_id(user_id, name)):
            skipped += 1
            continue
        credentials.set_token(_key_id(user_id, name), key)
        if not credentials.has_token(_key_id(user_id, name)):
            raise RuntimeError("keychain write could not be verified")
        mine.setdefault("options", {})["apiKeyRef"] = _KEY_REF
        imported += 1
    if imported:
        _write_providers(user_id, own)
        reload(user_id)
    _shred(path)
    return {"imported": imported, "skipped": skipped, "removed": True}
