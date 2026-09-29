import asyncio
import re
import sys
from typing import Optional
from urllib.parse import urlparse

import httpx

from .constants import _GOOGLE_HOSTS, HTTP_TIMEOUT_S

_RM_RX = re.compile(r'resource_metadata="([^"]+)"')


def _is_google(url: str) -> bool:
    return (urlparse(url or "").hostname or "") in _GOOGLE_HOSTS


def _guard(url: str, owner: Optional[int]) -> None:
    """Personal servers' URLs are user-controlled: keep discovery off internal hosts."""
    u = urlparse(url or "")
    if u.scheme != "https":
        if not (owner is None and u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost")):
            raise ValueError(f"oauth endpoint must be https: {url}")
    if owner is not None:
        from ..net_guard import check_url
        check_url(url)


async def _get_json(client: httpx.AsyncClient, url: str, owner: Optional[int]) -> Optional[dict]:
    try:
        guard_fn = getattr(sys.modules.get("core.mcp_oauth"), "_guard", _guard)
        await asyncio.get_running_loop().run_in_executor(None, guard_fn, url, owner)
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
