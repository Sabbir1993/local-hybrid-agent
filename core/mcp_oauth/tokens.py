import asyncio
import json
import sys
import time
from typing import Optional

import httpx

from .constants import (
    DCR_KEY,
    HTTP_TIMEOUT_S,
    REFRESH_MARGIN_S,
    SECRET_KEY,
    TOKEN_KEY,
    _ref,
)
from .discovery import _guard

_refresh_locks: dict = {}
_flow_status: dict = {}  # (owner, name) -> {"state": pending|done|error, "error": str, "at": ts}


def load_token(name: str, owner: Optional[int]) -> Optional[dict]:
    from .. import credentials
    raw = credentials.get_token(_ref(name, owner, TOKEN_KEY))
    if not raw:
        return None
    try:
        tok = json.loads(raw)
        return tok if isinstance(tok, dict) and tok.get("access_token") else None
    except ValueError:
        return None


def save_token(name: str, owner: Optional[int], tok: dict) -> None:
    from .. import credentials
    keep = {k: tok.get(k) for k in ("access_token", "refresh_token", "expires_at", "token_type",
                                    "scope", "token_endpoint", "client_id", "resource") if tok.get(k)}
    credentials.set_token(_ref(name, owner, TOKEN_KEY), json.dumps(keep))


def clear_token(name: str, owner: Optional[int]) -> None:
    from .. import credentials
    credentials.delete_token(_ref(name, owner, TOKEN_KEY))


def has_token(name: str, owner: Optional[int]) -> bool:
    return load_token(name, owner) is not None


def _client_secret(name: str, owner: Optional[int]) -> Optional[str]:
    from .. import credentials
    return credentials.get_token(_ref(name, owner, SECRET_KEY))


def _dcr_client(name: str, owner: Optional[int]) -> Optional[dict]:
    from .. import credentials
    raw = credentials.get_token(_ref(name, owner, DCR_KEY))
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


async def _token_request(token_url: str, data: dict, owner: Optional[int]) -> dict:
    guard_fn = getattr(sys.modules.get("core.mcp_oauth"), "_guard", _guard)
    await asyncio.get_running_loop().run_in_executor(None, guard_fn, token_url, owner)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
        r = await client.post(token_url, data=data, headers={"Accept": "application/json"})
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code >= 400 or not body.get("access_token"):
        raise RuntimeError(f"token endpoint refused: {body.get('error') or r.status_code}"
                           + (f" ({body.get('error_description')[:160]})" if body.get("error_description") else ""))
    return body


def _stamp(body: dict, flow_like: dict, prev: Optional[dict] = None) -> dict:
    tok = dict(body)
    if body.get("expires_in"):
        tok["expires_at"] = time.time() + float(body["expires_in"])
    tok["token_endpoint"] = flow_like["token_url"]
    tok["client_id"] = flow_like["client_id"]
    if flow_like.get("resource"):
        tok["resource"] = flow_like["resource"]
    if prev and not tok.get("refresh_token") and prev.get("refresh_token"):
        tok["refresh_token"] = prev["refresh_token"]
    return tok


async def access_token(name: str, owner: Optional[int], force_refresh: bool = False) -> Optional[str]:
    """Current access token (refreshed when it expires within a minute), or None."""
    tok = load_token(name, owner)
    if not tok:
        return None
    exp = float(tok.get("expires_at") or 0)
    if not force_refresh and (not exp or exp - time.time() > REFRESH_MARGIN_S):
        return tok["access_token"]
    if not tok.get("refresh_token") or not tok.get("token_endpoint"):
        return None if force_refresh else tok["access_token"]
    lock = _refresh_locks.setdefault((name, owner), asyncio.Lock())
    async with lock:
        cur = load_token(name, owner) or tok
        if not force_refresh and cur is not tok and float(cur.get("expires_at") or 0) - time.time() > REFRESH_MARGIN_S:
            return cur["access_token"]
        data = {"grant_type": "refresh_token", "refresh_token": cur["refresh_token"],
                "client_id": cur.get("client_id") or ""}
        secret = _client_secret(name, owner) or (_dcr_client(name, owner) or {}).get("client_secret")
        if secret:
            data["client_secret"] = secret
        if cur.get("resource"):
            data["resource"] = cur["resource"]
        tok_req_fn = getattr(sys.modules.get("core.mcp_oauth"), "_token_request", _token_request)
        try:
            body = await tok_req_fn(cur["token_endpoint"], data, owner)
        except Exception as e:
            print(f"[mcp-oauth] refresh for '{name}' failed: {e}", file=sys.stderr)
            if "invalid_grant" in str(e):
                clear_token(name, owner)
            return None
        new = _stamp(body, {"token_url": cur["token_endpoint"], "client_id": cur.get("client_id"),
                            "resource": cur.get("resource")}, prev=cur)
        save_token(name, owner, new)
        return new["access_token"]


def set_status(name: str, owner: Optional[int], state: str, error: str = "") -> None:
    _flow_status[(owner, name)] = {"state": state, "error": error, "at": time.time()}


def get_status(name: str, owner: Optional[int]) -> dict:
    st = dict(_flow_status.get((owner, name)) or {"state": "idle", "error": ""})
    st["signed_in"] = has_token(name, owner)
    return st
