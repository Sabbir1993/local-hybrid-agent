"""
core/net_guard.py - SSRF guard for server-side fetches of untrusted URLs.

The model (web_fetch) and knowledge-base ingest can ask the server to fetch
arbitrary URLs. Without a guard that reaches llama-server (127.0.0.1:8090),
this app's own API, LAN hosts, or cloud metadata endpoints. Every hop --
including each redirect -- must resolve only to public addresses.

Residual risk: DNS can change between the check and httpx's own lookup
(rebinding). Closing that fully needs an egress proxy / firewall rule; keep
this host network-isolated from anything in PCI scope regardless.
"""

import ipaddress
import socket
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx

_BLOCKED_HOSTNAMES = ("localhost", "localhost.localdomain", "metadata.google.internal")
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")
MAX_REDIRECTS = 5
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


class BlockedURLError(ValueError):
    pass


def _ip_blocked(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        # IPv4 embedded in IPv6 (::ffff:, 6to4 2002::/16, NAT64 64:ff9b::/96) reaches the v4 host
        if ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        elif ip.sixtofour:
            ip = ip.sixtofour
        elif ip in _NAT64:
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
            or ip.is_multicast or ip.is_unspecified or not ip.is_global)


def check_url(url: str) -> str:
    """Raise BlockedURLError unless `url` is http(s) and its host resolves only
    to public addresses. Returns the url unchanged."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise BlockedURLError("only http/https URLs are supported")
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise BlockedURLError("URL has no host")
    if host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_SUFFIXES):
        raise BlockedURLError(f"refusing to fetch internal host '{host}'")
    try:
        ip = ipaddress.ip_address(host)
        addrs = [ip]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80),
                                       proto=socket.IPPROTO_TCP)
        except socket.gaierror as e:
            raise BlockedURLError(f"cannot resolve host '{host}': {e}") from e
        addrs = [ipaddress.ip_address(info[4][0].split("%")[0]) for info in infos]
    for a in addrs:
        if _ip_blocked(a):
            raise BlockedURLError(f"refusing to fetch '{host}': resolves to non-public address {a}")
    return url


@dataclass
class FetchResult:
    url: str
    status_code: int
    headers: httpx.Headers
    content: bytes
    encoding: Optional[str] = None
    truncated: bool = False
    history: list = field(default_factory=list)

    @property
    def text(self) -> str:
        return self.content.decode(self.encoding or "utf-8", errors="replace")


def guarded_get(url: str, timeout: float, headers: Optional[dict] = None,
                max_bytes: int = 5 * 1024 * 1024) -> FetchResult:
    """Synchronous GET that validates every redirect hop and stops reading the
    body after `max_bytes` (never buffers an unbounded response)."""
    history = []
    with httpx.Client(follow_redirects=False, timeout=timeout, headers=headers or {}) as c:
        for _ in range(MAX_REDIRECTS + 1):
            check_url(url)
            with c.stream("GET", url) as r:
                if r.is_redirect and r.headers.get("location"):
                    history.append(url)
                    url = urljoin(url, r.headers["location"])
                    continue
                buf = bytearray()
                truncated = False
                for chunk in r.iter_bytes():
                    buf.extend(chunk)
                    if len(buf) >= max_bytes:
                        truncated = True
                        break
                return FetchResult(url=str(r.url), status_code=r.status_code, headers=r.headers,
                                   content=bytes(buf[:max_bytes]), encoding=r.encoding,
                                   truncated=truncated, history=history)
    raise BlockedURLError(f"too many redirects (> {MAX_REDIRECTS})")
