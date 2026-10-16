"""Talking to the code runner: https to a certificate we pin, never to a public address, never without a token.

The runner lives on a VM of its own (tools/runner). This module is the only place the app opens a connection to it."""
import asyncio
import hashlib
import hmac
import http.client
import ipaddress
import json
import socket
import ssl
from typing import Optional
from urllib.parse import urlparse

MAX_RESPONSE_BYTES = 100 * 1024 * 1024


class RunnerError(RuntimeError):
    """Something the admin can act on; the message is safe to show."""


def parse_url(url: str) -> tuple:
    u = urlparse(str(url or "").strip())
    if u.scheme != "https" or not u.hostname:
        raise RunnerError("the runner address must start with https:// (for example https://10.20.0.5:8470)")
    if u.username or u.password or u.path not in ("", "/") or u.query or u.fragment:
        raise RunnerError("the runner address is only a host and a port")
    return u.hostname, u.port or 8470


def check_private(host: str, allow_public: bool = False) -> None:
    """The runner belongs on a private network: refuse an address that is public, link-local (cloud metadata) or unresolvable."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        raise RunnerError(f"cannot resolve {host}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            raise RunnerError(f"{ip} is not a usable runner address")
        if not (ip.is_loopback or ip.is_private) and not allow_public:
            raise RunnerError(f"{ip} is a public address: put the runner on a private network "
                              "(or set sandbox.allow_public_runner if your firewall guarantees it)")


def fingerprint_of(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


def _request_sync(host: str, port: int, method: str, path: str, body: Optional[dict], token: Optional[str],
                  pin: Optional[str], timeout: float) -> dict:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE                     # trust is the pinned fingerprint, checked below, not a CA
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    conn = http.client.HTTPSConnection(host, port, context=ctx, timeout=timeout)
    try:
        try:
            conn.connect()
        except (OSError, ssl.SSLError) as e:
            raise RunnerError(f"cannot reach the runner ({type(e).__name__})")
        fp = fingerprint_of(conn.sock.getpeercert(binary_form=True))
        if pin and not hmac.compare_digest(fp, pin.lower()):
            raise RunnerError("the runner's certificate changed: it was reinstalled or something is in the way. "
                              "Pair it again from Settings if you reinstalled it")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        try:
            conn.request(method, path, body=payload, headers=headers)
            resp = conn.getresponse()
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
        except (OSError, http.client.HTTPException) as e:
            raise RunnerError(f"the runner stopped answering ({type(e).__name__})")
        if len(raw) > MAX_RESPONSE_BYTES:
            raise RunnerError("the runner's answer is too large")
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            raise RunnerError("the runner sent something that is not a result")
        return {"status": resp.status, "json": data if isinstance(data, dict) else {}, "fingerprint": fp}
    finally:
        conn.close()


async def request(url: str, method: str, path: str, body: Optional[dict] = None, token: Optional[str] = None,
                  pin: Optional[str] = None, timeout: float = 30, allow_public: bool = False) -> dict:
    host, port = parse_url(url)
    await asyncio.to_thread(check_private, host, allow_public)
    return await asyncio.to_thread(_request_sync, host, port, method, path, body, token, pin, timeout)


def pairing_proof(code: str, fingerprint: str) -> str:
    """HMAC(normalised code, certificate fingerprint): binds the one-time code to the certificate we were shown."""
    import re
    key = re.sub(r"[^A-Z0-9]", "", str(code or "").upper()).encode()
    return hmac.new(key, fingerprint.encode(), hashlib.sha256).hexdigest()
