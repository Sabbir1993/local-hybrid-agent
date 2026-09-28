"""OAuth 2.1 (authorization code + PKCE) for remote streamable-HTTP MCP servers.

Remote MCP servers such as Gmail's (https://gmailmcp.googleapis.com/mcp/v1) answer
initialize / tools/list anonymously but reject tools/call with 401 until the request
carries a user access token. This module:

  - discovers the authorization server: WWW-Authenticate resource_metadata, then
    RFC 9728 protected-resource metadata, then RFC 8414 / OIDC discovery; a server
    config's "auth" block can override any endpoint;
  - runs the code + PKCE (S256) flow. The redirect has to reach the user's own
    device (Google only accepts loopback or public-https redirect URIs), so by
    default the paired companion listens on 127.0.0.1:<port>, opens the system
    browser and hands the code back ("oauth.loopback" op). A server-side callback
    is used only when capabilities.mcp_oauth_redirect_base (a public https URL of
    this app) is configured;
  - optionally registers a client (RFC 7591) when the server supports it and no
    client_id is configured;
  - keeps tokens per user in the OS keychain (core/credentials.py), never in
    app.json / auth.db, and refreshes them shortly before expiry.

Server config ("auth" block, secret-free):
  "auth": {"type": "oauth", "client_id": "...", "scopes": ["..."],
           "auth_url": "...", "token_url": "...",      # optional overrides
           "redirect_port": 0,                         # 0 = any free loopback port
           "extra_params": {"access_type": "offline"}}
  client secret (Google desktop clients need one) -> secret_env_keys "OAUTH_CLIENT_SECRET"
"""

import asyncio
import base64
import hashlib
import json
import re
import secrets
import sys
import time
from typing import Optional
from urllib.parse import urlencode, urlparse

import httpx

TOKEN_KEY = "OAUTH_TOKEN"
SECRET_KEY = "OAUTH_CLIENT_SECRET"
DCR_KEY = "OAUTH_CLIENT"             # dynamically registered client info (JSON)
DEFAULT_DCR_PORT = 33418             # DCR needs a redirect URI known before the flow starts
FLOW_TTL_S = 600
REFRESH_MARGIN_S = 60
HTTP_TIMEOUT_S = 20

_GOOGLE_HOSTS = ("accounts.google.com", "oauth2.googleapis.com")


class McpAuthRequired(RuntimeError):
    """The MCP server wants a (fresh) user sign-in."""


def _ref(name: str, owner: Optional[int], key: str) -> str:
    from .mcp import secret_env_ref
    return secret_env_ref(name, key, owner)


def auth_cfg(cfg: dict) -> dict:
    a = (cfg or {}).get("auth")
    return a if isinstance(a, dict) and a.get("type") == "oauth" else {}


# ---------------- token store (OS keychain) ----------------

def load_token(name: str, owner: Optional[int]) -> Optional[dict]:
    from . import credentials
    raw = credentials.get_token(_ref(name, owner, TOKEN_KEY))
    if not raw:
        return None
    try:
        tok = json.loads(raw)
        return tok if isinstance(tok, dict) and tok.get("access_token") else None
    except ValueError:
        return None


def save_token(name: str, owner: Optional[int], tok: dict) -> None:
    from . import credentials
    keep = {k: tok.get(k) for k in ("access_token", "refresh_token", "expires_at", "token_type",
                                    "scope", "token_endpoint", "client_id", "resource") if tok.get(k)}
    credentials.set_token(_ref(name, owner, TOKEN_KEY), json.dumps(keep))


def clear_token(name: str, owner: Optional[int]) -> None:
    from . import credentials
    credentials.delete_token(_ref(name, owner, TOKEN_KEY))


def has_token(name: str, owner: Optional[int]) -> bool:
    return load_token(name, owner) is not None


def _client_secret(name: str, owner: Optional[int]) -> Optional[str]:
    from . import credentials
    return credentials.get_token(_ref(name, owner, SECRET_KEY))


def _dcr_client(name: str, owner: Optional[int]) -> Optional[dict]:
    from . import credentials
    raw = credentials.get_token(_ref(name, owner, DCR_KEY))
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


# ---------------- discovery ----------------

def _is_google(url: str) -> bool:
    return (urlparse(url or "").hostname or "") in _GOOGLE_HOSTS


def _guard(url: str, owner: Optional[int]) -> None:
    """Personal servers' URLs are user-controlled: keep discovery off internal hosts."""
    u = urlparse(url or "")
    if u.scheme != "https":
        if not (owner is None and u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost")):
            raise ValueError(f"oauth endpoint must be https: {url}")
    if owner is not None:
        from .net_guard import check_url
        check_url(url)


async def _get_json(client: httpx.AsyncClient, url: str, owner: Optional[int]) -> Optional[dict]:
    try:
        await asyncio.get_running_loop().run_in_executor(None, _guard, url, owner)
        r = await client.get(url, headers={"Accept": "application/json"})
        if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
            d = r.json()
            return d if isinstance(d, dict) else None
    except Exception as e:
        print(f"[mcp-oauth] discovery {url}: {type(e).__name__}: {e}", file=sys.stderr)
    return None


def _well_known(base: str, suffix: str) -> list:
    """RFC 8414/9728 path insertion first, then the plain root form."""
    u = urlparse(base)
    root = f"{u.scheme}://{u.netloc}"
    path = u.path.rstrip("/")
    out = [f"{root}/.well-known/{suffix}{path}"] if path else []
    out.append(f"{root}/.well-known/{suffix}")
    return out


_RM_RX = re.compile(r'resource_metadata="([^"]+)"')


async def discover(server_url: str, owner: Optional[int] = None, www_authenticate: str = "") -> dict:
    """-> {authorization_endpoint, token_endpoint, registration_endpoint?, scopes_supported?, resource}"""
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S, follow_redirects=False) as client:
        prm = None
        cands = []
        m = _RM_RX.search(www_authenticate or "")
        if m:
            cands.append(m.group(1))
        cands += _well_known(server_url, "oauth-protected-resource")
        for c in cands:
            prm = await _get_json(client, c, owner)
            if prm and prm.get("authorization_servers"):
                break
        issuer = ((prm or {}).get("authorization_servers") or [None])[0]
        if not issuer:
            u = urlparse(server_url)
            issuer = f"{u.scheme}://{u.netloc}"
        asm = None
        for c in _well_known(issuer, "oauth-authorization-server") + _well_known(issuer, "openid-configuration"):
            asm = await _get_json(client, c, owner)
            if asm and asm.get("authorization_endpoint") and asm.get("token_endpoint"):
                break
            asm = None
    out = {
        "issuer": issuer,
        "resource": (prm or {}).get("resource") or server_url,
        "scopes_supported": (prm or {}).get("scopes_supported") or (asm or {}).get("scopes_supported") or [],
    }
    if asm:
        for k in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
            if asm.get(k):
                out[k] = asm[k]
    return out


# ---------------- flow ----------------

_flows: dict = {}        # state -> flow dict
_flow_status: dict = {}  # (owner, name) -> {"state": pending|done|error, "error": str, "at": ts}


def _prune() -> None:
    now = time.time()
    for s in [s for s, f in _flows.items() if f["expires"] < now]:
        _flows.pop(s, None)


def _pkce() -> tuple:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


REDIRECT_PLACEHOLDER = "__A770_REDIRECT_URI__"


async def _register_client(meta: dict, name: str, owner: Optional[int], redirect_uri: str) -> dict:
    from . import credentials
    body = {"client_name": "A770 Runtime", "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
            "token_endpoint_auth_method": "none"}
    await asyncio.get_running_loop().run_in_executor(None, _guard, meta["registration_endpoint"], owner)
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
    """Build the authorization URL. redirect_uri=None -> companion loopback: the URL then
    carries REDIRECT_PLACEHOLDER, which the companion swaps for its own 127.0.0.1 URI.
    -> {"auth_url", "state", "loopback_port"}"""
    _prune()
    a = auth_cfg(cfg)
    meta = await discover(cfg.get("url", ""), owner)
    auth_url = a.get("auth_url") or meta.get("authorization_endpoint")
    token_url = a.get("token_url") or meta.get("token_endpoint")
    if not auth_url or not token_url:
        raise RuntimeError("could not discover the server's OAuth endpoints; set auth_url and token_url")
    for u in (auth_url, token_url):
        await asyncio.get_running_loop().run_in_executor(None, _guard, u, owner)

    port = int(a.get("redirect_port") or 0)
    client_id = a.get("client_id")
    client_secret = _client_secret(name, owner)
    if not client_id:
        reg = _dcr_client(name, owner)
        if not reg and meta.get("registration_endpoint"):
            port = port or DEFAULT_DCR_PORT
            reg = await _register_client(meta, name, owner, redirect_uri or f"http://127.0.0.1:{port}/callback")
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
        # refresh token + re-consent so a refresh token is always issued
        params.update({"access_type": "offline", "prompt": "consent"})
    for k, v in (a.get("extra_params") or {}).items():
        params[str(k)] = str(v)
    _flows[state] = {
        "name": name, "owner": owner, "verifier": verifier, "token_url": token_url,
        "client_id": client_id, "client_secret": client_secret, "redirect_uri": redirect_uri,
        "resource": params.get("resource"), "expires": time.time() + FLOW_TTL_S,
    }
    return {"auth_url": f"{auth_url}{'&' if '?' in auth_url else '?'}{urlencode(params)}",
            "state": state, "loopback_port": port}


async def _token_request(token_url: str, data: dict, owner: Optional[int]) -> dict:
    await asyncio.get_running_loop().run_in_executor(None, _guard, token_url, owner)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
        r = await client.post(token_url, data=data, headers={"Accept": "application/json"})
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code >= 400 or not body.get("access_token"):
        # error codes only -- never echo tokens/bodies into logs or the UI
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
        tok["refresh_token"] = prev["refresh_token"]    # most servers don't rotate it
    return tok


async def complete(state: str, code: str, redirect_uri: Optional[str] = None,
                   expect_owner: Optional[int] = None, check_owner: bool = False) -> dict:
    """Exchange the code for tokens and store them. -> {"name", "owner"}"""
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
    body = await _token_request(flow["token_url"], data, flow["owner"])
    save_token(flow["name"], flow["owner"], _stamp(body, flow))
    return {"name": flow["name"], "owner": flow["owner"]}


_refresh_locks: dict = {}


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
            return cur["access_token"]      # another request refreshed it meanwhile
        data = {"grant_type": "refresh_token", "refresh_token": cur["refresh_token"],
                "client_id": cur.get("client_id") or ""}
        secret = _client_secret(name, owner) or (_dcr_client(name, owner) or {}).get("client_secret")
        if secret:
            data["client_secret"] = secret
        if cur.get("resource"):
            data["resource"] = cur["resource"]
        try:
            body = await _token_request(cur["token_endpoint"], data, owner)
        except Exception as e:
            print(f"[mcp-oauth] refresh for '{name}' failed: {e}", file=sys.stderr)
            if "invalid_grant" in str(e):
                clear_token(name, owner)     # revoked / expired refresh token: sign in again
            return None
        new = _stamp(body, {"token_url": cur["token_endpoint"], "client_id": cur.get("client_id"),
                            "resource": cur.get("resource")}, prev=cur)
        save_token(name, owner, new)
        return new["access_token"]


# ---------------- UI flow status ----------------

def set_status(name: str, owner: Optional[int], state: str, error: str = "") -> None:
    _flow_status[(owner, name)] = {"state": state, "error": error, "at": time.time()}


def get_status(name: str, owner: Optional[int]) -> dict:
    st = dict(_flow_status.get((owner, name)) or {"state": "idle", "error": ""})
    st["signed_in"] = has_token(name, owner)
    return st


# ---------------- plaintext-secret migration ----------------

_ID_KEYS = {"clientid", "client_id", "oauth_client_id", "google_client_id"}
_SECRET_KEYS = {"clientsecret", "client_secret", "oauth_client_secret", "google_client_secret"}


def migrate_plain_client(name: str, owner: Optional[int], cfg: dict) -> Optional[dict]:
    """An http server saved with clientId/clientSecret as plain env values (they were
    never sent anywhere, and the secret sat in the config in the clear): move the id into
    an oauth "auth" block and the secret into the keychain. -> new cfg, or None if unchanged."""
    if (cfg.get("transport") or ("stdio" if cfg.get("command") else "http")) != "http":
        return None
    env = dict(cfg.get("env") or {})
    id_k = next((k for k in env if k.lower() in _ID_KEYS), None)
    sec_k = next((k for k in env if k.lower() in _SECRET_KEYS), None)
    if not id_k and not sec_k:
        return None
    from . import credentials
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
