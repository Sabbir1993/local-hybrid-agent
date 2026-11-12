import re
import ssl
from typing import Optional
from urllib.parse import urlsplit

import httpx

_HTTP_RX = re.compile(r"mcp http (\d{3})")


def _host(cfg: dict) -> str:
    try:
        return urlsplit(str(cfg.get("url") or "")).hostname or "the server"
    except ValueError:
        return "the server"


def explain_connect_error(exc: BaseException, cfg: dict) -> Optional[str]:
    """One plain line saying WHY an http connector failed to connect, or None when we have nothing better than the raw
    message. Only the host name is ever quoted - never the URL's query, a header, a token or a response body - so the line
    is safe to show in the connector card. Every branch tells the person what to check next."""
    from ..mcp_oauth import McpAuthRequired
    from ..net_guard import BlockedURLError
    host = _host(cfg)
    if isinstance(exc, McpAuthRequired):
        return f"{host} needs you to sign in - press Connect account."
    if isinstance(exc, BlockedURLError):
        m = str(exc)
        if m.startswith("cannot resolve"):
            return f"DNS lookup failed for {host}: this server cannot resolve it - check the connector URL and the server's DNS."
        return f"{host} was blocked by the server's network safety check (internal or non-public address)."
    if isinstance(exc, httpx.TimeoutException):
        return (f"Timed out reaching {host}: this server may be blocked from reaching it on the internet "
                "(firewall or proxy), or the provider is slow.")
    if isinstance(exc, httpx.ConnectError):
        chain, e = [], exc
        while e is not None and len(chain) < 6:
            chain.append(e)
            e = e.__cause__ or e.__context__
        if any(isinstance(c, ssl.SSLError) for c in chain) or "CERTIFICATE" in str(exc).upper():
            return (f"TLS certificate check failed for {host}: a proxy that re-signs HTTPS needs its CA installed "
                    "on this server, or the URL is wrong.")
        if any("name or service not known" in str(c).lower() or "getaddrinfo" in str(c).lower()
               or "temporary failure in name resolution" in str(c).lower() for c in chain):
            return f"DNS lookup failed for {host}: this server cannot resolve it - check the connector URL and the server's DNS."
        return (f"Could not open a connection to {host}: outbound HTTPS from this server is probably blocked "
                "(firewall or proxy) or the address is wrong.")
    if isinstance(exc, httpx.TransportError):
        return f"Network error talking to {host} ({type(exc).__name__}): check this server's outbound connectivity."
    if isinstance(exc, ValueError):         # response was not JSON: a web page, or a legacy SSE endpoint
        return (f"{host} answered, but not with MCP: the URL may be a web page or the legacy SSE endpoint "
                "- check the connector URL and transport.")
    m = _HTTP_RX.search(str(exc))
    if m:
        code = int(m.group(1))
        if code == 403:
            return (f"{host} refused access (HTTP 403): the account or organization may not allow this app - "
                    "ask its admin to allow MCP clients.")
        if code == 404:
            return f"HTTP 404 from {host}: the connector URL is wrong or outdated."
        if code in (400, 405, 406, 415):
            return (f"{host} rejected the request (HTTP {code}): the URL may be wrong, or it speaks the legacy SSE "
                    "transport instead of streamable-http.")
        if code == 429:
            return f"{host} is rate limiting this server (HTTP 429): wait a minute and retry."
        if code >= 500:
            return f"{host} had an error (HTTP {code}): try again later."
    return None
