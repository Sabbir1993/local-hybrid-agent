import asyncio
import base64
import hashlib
import json
import secrets
import sys
import time
from typing import Optional
from urllib.parse import urlencode, urlparse

import httpx

from .constants import (
    DCR_KEY,
    DEFAULT_DCR_PORT,
    FLOW_TTL_S,
    HTTP_TIMEOUT_S,
    REDIRECT_PLACEHOLDER,
    SECRET_KEY,
    _ID_KEYS,
    _SECRET_KEYS,
    _ref,
    auth_cfg,
)
from .discovery import _guard, _is_google, discover
from .tokens import _client_secret, _dcr_client, _stamp, _token_request, save_token

_flows: dict = {}        # state -> flow dict


def _pkg():
    return sys.modules.get("core.mcp_oauth")


def _prune() -> None:
    now = time.time()
    for s in [s for s, f in _flows.items() if f["expires"] < now]:
        _flows.pop(s, None)


def _pkce() -> tuple:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


async def _register_client(meta: dict, name: str, owner: Optional[int], redirect_uri: str) -> dict:
    from .. import credentials
    body = {"client_name": "A770 Runtime", "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
            "token_endpoint_auth_method": "none"}
    guard_fn = getattr(_pkg(), "_guard", _guard)
    await asyncio.get_running_loop().run_in_executor(None, guard_fn, meta["registration_endpoint"], owner)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
        r = await client.post(meta["registration_endpoint"], json=body)
    if r.status_code >= 400:
        raise RuntimeError(f"client registration failed (HTTP {r.status_code})")
    info = r.json()
    if not info.get("client_id"):
        raise RuntimeError("client registration returned no client_id")
    info["redirect_uri"] = redirect_uri
    credentials.set_token(_ref(name, owner, DCR_KEY), json.dumps(
        {k: info.get(k) for k in ("client_id", "client_secret", "redirect_uri") if info.get(k)}))
    return info


async def begin(name: str, owner: Optional[int], cfg: dict, redirect_uri: Optional[str] = None) -> dict:
    _prune()
    a = auth_cfg(cfg)
    discover_fn = getattr(_pkg(), "discover", discover)
    meta = await discover_fn(cfg.get("url", ""), owner)
    auth_url = a.get("auth_url") or meta.get("authorization_endpoint")
    token_url = a.get("token_url") or meta.get("token_endpoint")
    if not auth_url or not token_url:
        raise RuntimeError("could not discover the server's OAuth endpoints; set auth_url and token_url")
    guard_fn = getattr(_pkg(), "_guard", _guard)
    for u in (auth_url, token_url):
        await asyncio.get_running_loop().run_in_executor(None, guard_fn, u, owner)

    port = int(a.get("redirect_port") or 0)
    client_id = a.get("client_id")
    client_secret = _client_secret(name, owner)
    shared = None
    if not client_id:
        from .. import mcp_catalog
        shared = mcp_catalog.shared_client(name, cfg)
        if shared:
            client_id, client_secret = shared["client_id"], shared["secret"] or client_secret
    if not client_id:
        reg = _dcr_client(name, owner)
        if not reg and meta.get("registration_endpoint"):
            port = port or DEFAULT_DCR_PORT
            reg_client_fn = getattr(_pkg(), "_register_client", _register_client)
            reg = await reg_client_fn(meta, name, owner, redirect_uri or f"http://127.0.0.1:{port}/callback")
        if not reg:
            raise RuntimeError("this server needs an OAuth client id (it does not support client registration)")
        client_id = reg["client_id"]
        client_secret = reg.get("client_secret") or client_secret
        if not redirect_uri and reg.get("redirect_uri"):
            port = urlparse(reg["redirect_uri"]).port or port

    scopes = a.get("scopes") or []
    if isinstance(scopes, str):
        scopes = scopes.split()
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(24)
    params = {
        "response_type": "code", "client_id": client_id,
        "redirect_uri": redirect_uri or REDIRECT_PLACEHOLDER,
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
    }
    if scopes:
        params["scope"] = " ".join(scopes)
    send_resource = a.get("resource_param", not _is_google(auth_url))
    if send_resource:
        params["resource"] = meta.get("resource") or cfg.get("url")
    if _is_google(auth_url):
        params.update({"access_type": "offline", "prompt": "consent"})
    for k, v in (a.get("extra_params") or {}).items():
        params[str(k)] = str(v)
    _flows[state] = {
        "name": name, "owner": owner, "verifier": verifier, "token_url": token_url,
        "client_id": client_id, "client_secret": client_secret, "redirect_uri": redirect_uri,
        "resource": params.get("resource"), "expires": time.time() + FLOW_TTL_S,
        "shared_client": bool(shared),
    }
    return {"auth_url": f"{auth_url}{'&' if '?' in auth_url else '?'}{urlencode(params)}",
            "state": state, "loopback_port": port}


async def complete(state: str, code: str, redirect_uri: Optional[str] = None,
                   expect_owner: Optional[int] = None, check_owner: bool = False) -> dict:
    _prune()
    flow = _flows.pop(state or "", None)
    if not flow:
        raise RuntimeError("sign-in expired or was already used - start again")
    if check_owner and flow["owner"] != expect_owner:
        raise RuntimeError("this sign-in belongs to another user")
    data = {"grant_type": "authorization_code", "code": code,
            "redirect_uri": redirect_uri or flow["redirect_uri"],
            "client_id": flow["client_id"], "code_verifier": flow["verifier"]}
    if flow.get("client_secret"):
        data["client_secret"] = flow["client_secret"]
    if flow.get("resource"):
        data["resource"] = flow["resource"]
    tok_req_fn = getattr(_pkg(), "_token_request", _token_request)
    body = await tok_req_fn(flow["token_url"], data, flow["owner"])
    save_token(flow["name"], flow["owner"], _stamp(body, flow))
    return {"name": flow["name"], "owner": flow["owner"]}


def migrate_plain_client(name: str, owner: Optional[int], cfg: dict) -> Optional[dict]:
    if (cfg.get("transport") or ("stdio" if cfg.get("command") else "http")) != "http":
        return None
    env = dict(cfg.get("env") or {})
    id_k = next((k for k in env if k.lower() in _ID_KEYS), None)
    sec_k = next((k for k in env if k.lower() in _SECRET_KEYS), None)
    if not id_k and not sec_k:
        return None
    from .. import credentials
    new = dict(cfg)
    auth = dict(auth_cfg(cfg) or {"type": "oauth"})
    if id_k:
        auth.setdefault("client_id", str(env.pop(id_k)))
    if sec_k:
        val = str(env.pop(sec_k))
        if val:
            credentials.set_token(_ref(name, owner, SECRET_KEY), val)
            new["secret_env_keys"] = sorted(set(new.get("secret_env_keys") or []) | {SECRET_KEY})
    if "gmailmcp.googleapis.com" in (cfg.get("url") or "") and not auth.get("scopes"):
        auth["scopes"] = ["https://www.googleapis.com/auth/gmail.readonly",
                          "https://www.googleapis.com/auth/gmail.compose"]
    new["auth"] = auth
    if env:
        new["env"] = env
    else:
        new.pop("env", None)
    return new
